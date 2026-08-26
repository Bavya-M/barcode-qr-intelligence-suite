import os
import io
import json
import numpy as np
import cv2
from PIL import Image
from flask import Flask, request, jsonify, render_template

app = Flask(__name__, template_folder='templates')

# Try importing pyzbar safely (handles missing system libzbar gracefully)
PYZBAR_AVAILABLE = False
try:
    from pyzbar import pyzbar
    PYZBAR_AVAILABLE = True
except Exception as e:
    PYZBAR_AVAILABLE = False
    print(f"Warning: pyzbar library could not be loaded: {e}")

def preprocess_image_variants(image_np):
    """
    Generates a list of preprocessed image variants optimized for certificate scanning.
    Includes resolution scaling, grayscale conversion, CLAHE contrast enhancement,
    sharpening, and adaptive thresholding.
    """
    variants = []
    h, w = image_np.shape[:2]

    # Helper to calculate scale factor for high-resolution images
    scales = [1.0]
    max_dim = max(h, w)
    if max_dim > 1600:
        scales.append(1600.0 / max_dim)
    if max_dim > 1000:
        scales.append(1000.0 / max_dim)

    for scale in scales:
        if scale == 1.0:
            resized = image_np
        else:
            new_w = int(w * scale)
            new_h = int(h * scale)
            resized = cv2.resize(image_np, (new_w, new_h), interpolation=cv2.INTER_AREA)

        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY) if len(resized.shape) == 3 else resized

        # Variant A: Standard Gray
        variants.append((gray, scale))

        # Variant B: CLAHE Contrast Enhancement
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        variants.append((enhanced, scale))

        # Variant C: Sharpened Image (enhances fine QR module edges on certificate scans)
        sharpen_kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        sharpened = cv2.filter2D(gray, -1, sharpen_kernel)
        variants.append((sharpened, scale))

        # Variant D: Otsu Thresholding
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append((otsu, scale))

        # Variant E: Adaptive Gaussian Thresholding
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        variants.append((adaptive, scale))

    return variants

def decode_opencv_fallback(image_np, scale=1.0):
    """
    Fallback decoding engine using native OpenCV WeChatQRCode, QRCodeDetector, and BarcodeDetector.
    Coordinates are scaled back to the original image dimensions.
    """
    results = []

    # 1. WeChat QR Code Detector (High precision detector if opencv-contrib is available)
    if hasattr(cv2, 'wechat_qrcode_WeChatQRCode'):
        try:
            detector = cv2.wechat_qrcode_WeChatQRCode()
            res, points = detector.detectAndDecode(image_np)
            for info, pts in zip(res, points):
                if info and len(pts) >= 4:
                    pts_orig = (pts / scale).astype(int)
                    min_x, min_y = np.min(pts_orig, axis=0)
                    max_x, max_y = np.max(pts_orig, axis=0)
                    results.append({
                        "symbology": "QRCODE",
                        "payload": info,
                        "bounding_box": {
                            "x": int(min_x),
                            "y": int(min_y),
                            "width": int(max_x - min_x),
                            "height": int(max_y - min_y)
                        },
                        "polygon": [{"x": int(pt[0]), "y": int(pt[1])} for pt in pts_orig]
                    })
        except Exception as e:
            print(f"WeChatQRCode detector notice: {e}")

    # 2. Standard OpenCV QRCodeDetector
    if not results:
        try:
            qr_detector = cv2.QRCodeDetector()
            retval, decoded_info, points, _ = qr_detector.detectAndDecodeMulti(image_np)
            if retval and decoded_info:
                for info, pts in zip(decoded_info, points):
                    if info and len(pts) >= 4:
                        pts_orig = (pts / scale).astype(int)
                        min_x, min_y = np.min(pts_orig, axis=0)
                        max_x, max_y = np.max(pts_orig, axis=0)
                        results.append({
                            "symbology": "QRCODE",
                            "payload": info,
                            "bounding_box": {
                                "x": int(min_x),
                                "y": int(min_y),
                                "width": int(max_x - min_x),
                                "height": int(max_y - min_y)
                            },
                            "polygon": [{"x": int(pt[0]), "y": int(pt[1])} for pt in pts_orig]
                        })
        except Exception as e:
            print(f"OpenCV QRCodeDetector notice: {e}")

    # 3. OpenCV Native Barcode Detector (1D Barcodes)
    if not results and hasattr(cv2, 'barcode'):
        try:
            barcode_detector = cv2.barcode.BarcodeDetector()
            ok, decoded_info, decoded_type, points = barcode_detector.detectAndDecode(image_np)
            if ok and decoded_info:
                for info, b_type, pts in zip(decoded_info, decoded_type, points):
                    if info and len(pts) >= 4:
                        pts_orig = (pts / scale).astype(int)
                        min_x, min_y = np.min(pts_orig, axis=0)
                        max_x, max_y = np.max(pts_orig, axis=0)
                        results.append({
                            "symbology": b_type if b_type else "BARCODE",
                            "payload": info,
                            "bounding_box": {
                                "x": int(min_x),
                                "y": int(min_y),
                                "width": int(max_x - min_x),
                                "height": int(max_y - min_y)
                            },
                            "polygon": [{"x": int(pt[0]), "y": int(pt[1])} for pt in pts_orig]
                        })
        except Exception as e:
            print(f"OpenCV BarcodeDetector notice: {e}")

    return results

def decode_barcodes(image_np):
    """
    Decodes 1D/2D barcodes from high-resolution certificate images and standard inputs.
    Uses PyZBar with multi-resolution preprocessing + OpenCV secondary fallback decoders.
    Combines and deduplicates all decoded results.
    """
    raw_results = []
    seen_payloads = set()

    # Preprocess image variants (original + downscaled + contrast enhanced)
    image_variants = preprocess_image_variants(image_np)

    # Engine 1: PyZBar Scanner across all preprocessed variants
    if PYZBAR_AVAILABLE:
        for img_variant, scale in image_variants:
            try:
                decoded_objects = pyzbar.decode(img_variant)
                for obj in decoded_objects:
                    try:
                        payload = obj.data.decode("utf-8")
                    except UnicodeDecodeError:
                        payload = obj.data.decode("latin-1", errors="replace")

                    if not payload or payload in seen_payloads:
                        continue

                    seen_payloads.add(payload)

                    rect = obj.rect
                    bbox = {
                        "x": int(rect.left / scale),
                        "y": int(rect.top / scale),
                        "width": int(rect.width / scale),
                        "height": int(rect.height / scale)
                    }

                    polygon = []
                    if obj.polygon:
                        polygon = [{"x": int(pt.x / scale), "y": int(pt.y / scale)} for pt in obj.polygon]

                    raw_results.append({
                        "symbology": obj.type,
                        "payload": payload,
                        "bounding_box": bbox,
                        "polygon": polygon
                    })
            except Exception as pyzbar_err:
                print(f"PyZBar execution notice: {pyzbar_err}")

    # Engine 2: Secondary OpenCV Fallback (QRCodeDetector / WeChatQRCode / BarcodeDetector)
    if not raw_results:
        for img_variant, scale in image_variants:
            cv_results = decode_opencv_fallback(img_variant, scale=scale)
            for res in cv_results:
                payload = res.get("payload")
                if payload and payload not in seen_payloads:
                    seen_payloads.add(payload)
                    raw_results.append(res)
            if raw_results:
                break

    return raw_results, None

@app.route("/", methods=["GET"])
def index():
    """Serves the Web Single-Page Application interface."""
    return render_template("index.html")

@app.route("/api/scan", methods=["POST"])
def scan_barcode():
    """
    POST Endpoint: Accepts image payload via multipart/form-data.
    Returns structured JSON with status, symbology, payload, and coordinates.
    """
    if "image" not in request.files and "file" not in request.files:
        return jsonify({
            "status": "error",
            "message": "Missing image payload. Please send an image file under the 'image' or 'file' key."
        }), 400

    file = request.files.get("image") or request.files.get("file")

    if not file or file.filename == "":
        return jsonify({
            "status": "error",
            "message": "Empty file received. Please upload a valid image."
        }), 400

    try:
        # Read image file stream into NumPy array
        file_bytes = np.frombuffer(file.read(), np.uint8)
        image_np = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)

        if image_np is None:
            # Fallback PIL decode if OpenCV imdecode returns None
            file.seek(0)
            pil_img = Image.open(io.BytesIO(file.read())).convert("RGB")
            image_np = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

        img_height, img_width = image_np.shape[:2]

        # Decode barcodes
        results, err_msg = decode_barcodes(image_np)

        if err_msg:
            return jsonify({
                "status": "error",
                "message": err_msg
            }), 500

        if not results:
            return jsonify({
                "status": "no_barcode_found",
                "count": 0,
                "message": "No 1D or 2D barcodes detected in the image.",
                "image_dimensions": {"width": img_width, "height": img_height},
                "results": []
            }), 200

        return jsonify({
            "status": "success",
            "count": len(results),
            "message": f"Successfully decoded {len(results)} barcode(s).",
            "image_dimensions": {"width": img_width, "height": img_height},
            "results": results
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"An error occurred while processing the image: {str(e)}"
        }), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
