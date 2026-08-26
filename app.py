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

def decode_opencv_fallback(image_np):
    """
    Fallback decoding engine using native OpenCV QRCodeDetector and BarcodeDetector.
    Eliminates dependency on native C-libraries (libzbar).
    """
    results = []

    # 1. OpenCV Native QR Code Detector
    try:
        qr_detector = cv2.QRCodeDetector()
        retval, decoded_info, points, _ = qr_detector.detectAndDecodeMulti(image_np)
        if retval and decoded_info:
            for info, pts in zip(decoded_info, points):
                if info and len(pts) >= 4:
                    pts_int = pts.astype(int)
                    min_x, min_y = np.min(pts_int, axis=0)
                    max_x, max_y = np.max(pts_int, axis=0)
                    results.append({
                        "symbology": "QRCODE",
                        "payload": info,
                        "bounding_box": {
                            "x": int(min_x),
                            "y": int(min_y),
                            "width": int(max_x - min_x),
                            "height": int(max_y - min_y)
                        },
                        "polygon": [{"x": int(pt[0]), "y": int(pt[1])} for pt in pts_int]
                    })
    except Exception as e:
        print(f"OpenCV QRCodeDetector fallback notice: {e}")

    # 2. OpenCV Native Barcode Detector (1D Barcodes)
    if not results and hasattr(cv2, 'barcode'):
        try:
            barcode_detector = cv2.barcode.BarcodeDetector()
            ok, decoded_info, decoded_type, points = barcode_detector.detectAndDecode(image_np)
            if ok and decoded_info:
                for info, b_type, pts in zip(decoded_info, decoded_type, points):
                    if info and len(pts) >= 4:
                        pts_int = pts.astype(int)
                        min_x, min_y = np.min(pts_int, axis=0)
                        max_x, max_y = np.max(pts_int, axis=0)
                        results.append({
                            "symbology": b_type if b_type else "BARCODE",
                            "payload": info,
                            "bounding_box": {
                                "x": int(min_x),
                                "y": int(min_y),
                                "width": int(max_x - min_x),
                                "height": int(max_y - min_y)
                            },
                            "polygon": [{"x": int(pt[0]), "y": int(pt[1])} for pt in pts_int]
                        })
        except Exception as e:
            print(f"OpenCV BarcodeDetector fallback notice: {e}")

    return results

def decode_barcodes(image_np):
    """
    Decodes 1D/2D barcodes from an OpenCV image buffer.
    Combines PyZBar engine (when available) with OpenCV native detector fallbacks.
    """
    results = []

    # Engine 1: PyZBar (if native libzbar library is loaded)
    if PYZBAR_AVAILABLE:
        try:
            # Stage 1: Direct decode on original image
            decoded_objects = pyzbar.decode(image_np)

            # Stage 2: Preprocessing fallback if no barcodes found
            if not decoded_objects:
                gray = cv2.cvtColor(image_np, cv2.COLOR_BGR2GRAY) if len(image_np.shape) == 3 else image_np
                decoded_objects = pyzbar.decode(gray)

                # Stage 3: Contrast enhancement (CLAHE) fallback
                if not decoded_objects:
                    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                    enhanced = clahe.apply(gray)
                    decoded_objects = pyzbar.decode(enhanced)

                # Stage 4: Adaptive thresholding fallback
                if not decoded_objects:
                    thresh = cv2.adaptiveThreshold(
                        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
                    )
                    decoded_objects = pyzbar.decode(thresh)

            for obj in decoded_objects:
                symbology = obj.type
                try:
                    payload = obj.data.decode("utf-8")
                except UnicodeDecodeError:
                    payload = obj.data.decode("latin-1", errors="replace")

                rect = obj.rect
                bbox = {
                    "x": int(rect.left),
                    "y": int(rect.top),
                    "width": int(rect.width),
                    "height": int(rect.height)
                }

                polygon = []
                if obj.polygon:
                    polygon = [{"x": int(pt.x), "y": int(pt.y)} for pt in obj.polygon]

                results.append({
                    "symbology": symbology,
                    "payload": payload,
                    "bounding_box": bbox,
                    "polygon": polygon
                })
        except Exception as pyzbar_err:
            print(f"PyZBar execution notice: {pyzbar_err}")

    # Engine 2: Pure OpenCV fallback if PyZBar is unavailable or found nothing
    if not results:
        results = decode_opencv_fallback(image_np)

        # Apply preprocessing (grayscale & contrast) with OpenCV fallback if needed
        if not results:
            gray = cv2.cvtColor(image_np, cv2.COLOR_BGR2GRAY) if len(image_np.shape) == 3 else image_np
            results = decode_opencv_fallback(gray)

            if not results:
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                enhanced = clahe.apply(gray)
                results = decode_opencv_fallback(enhanced)

    return results, None

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
