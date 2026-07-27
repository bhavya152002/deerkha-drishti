"""
rfdetr_trt.py

Drop-in TensorRT replacement for RFDETRBase.predict().

Why NMS is even a question here:
RF-DETR is a DETR-family, set-prediction model. Unlike YOLO/anchor-based
detectors, it's trained (Hungarian matching) to output ONE box per object
directly, so classic NMS is not required for correctness. What actually
happens after TensorRT / FP16 export is a handful of near-duplicate boxes
around real objects from quantization noise -- not systematic duplicate
detections. So the NMS here is a light safety net only: high IoU threshold
(0.7 default) so it never merges two genuinely separate, closely-spaced
animals (e.g. a herd) into a single box.

Usage (drop-in for your pipeline):
    model = RFDETR_TRT(RFDETR_ENGINE_PATH, apply_nms=True, nms_iou=0.7)
    dets = model.predict(rgb_frame, threshold=0.25)
    # dets.xyxy, dets.confidence, dets.class_id -- same shape/contract as
    # supervision.Detections from RFDETRBase, so nothing downstream in your
    # 8-cam script needs to change.

IMPORTANT -- class order:
MY_CLASSES below MUST exactly match the class order used when you exported
the .onnx -> .engine (i.e. the order of your training dataset.yaml / label
map), NOT necessarily the order in any comparison script. Verify this before
going live -- a silent mismatch here means correct-looking but wrong-species
alerts (e.g. tiger reported as leopard), each carrying its own threshold.
"""

import logging
import threading
import numpy as np
import cv2
import torch
import tensorrt as trt

log = logging.getLogger("DHEERKHA.rfdetr")

# Throttled: predict() runs per frame per camera (~10fps x 4 cams). An
# unthrottled traceback here would fill the disk. Falls back to a plain logger
# if log_setup isn't importable (e.g. this module used standalone in a notebook).
try:
    from log_setup import Throttle
    _THROTTLE = Throttle(log, burst=3, every=60.0)
except Exception:  # pragma: no cover
    class _Fallback:
        def exception(self, key, msg, *a):
            log.exception(msg, *a)

        def error(self, key, msg, *a):
            log.error(msg, *a)
    _THROTTLE = _Fallback()

# ---- VERIFY THIS AGAINST YOUR TRAINING CONFIG BEFORE DEPLOYMENT ----
MY_CLASSES = {0: "wild boar", 1: "elephant", 2: "leopard", 3: "tiger", 4: "person", 5: "deer"}
NUM_CLASSES = len(MY_CLASSES)

RF_INPUT_SIZE = 560
RF_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
RF_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class Detections:
    """Minimal stand-in for supervision.Detections, matching the fields your
    pipeline actually reads: .xyxy, .confidence, .class_id, len(), truthiness."""
    __slots__ = ("xyxy", "confidence", "class_id")

    def __init__(self, xyxy, confidence, class_id):
        self.xyxy = xyxy
        self.confidence = confidence
        self.class_id = class_id

    def __len__(self):
        return len(self.class_id)

    def __bool__(self):
        return len(self.class_id) > 0


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _iou_matrix(boxes):
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    n = len(boxes)
    iou = np.zeros((n, n), dtype=np.float32)
    for i in range(n):
        xx1 = np.maximum(x1[i], x1)
        yy1 = np.maximum(y1[i], y1)
        xx2 = np.minimum(x2[i], x2)
        yy2 = np.minimum(y2[i], y2)
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        union = areas[i] + areas - inter
        iou[i] = np.where(union > 0, inter / union, 0)
    return iou


def class_aware_nms(xyxy, confidence, class_id, iou_thresh=0.7):
    if len(xyxy) == 0:
        return xyxy, confidence, class_id
    keep = []
    for cls in np.unique(class_id):
        idx = np.where(class_id == cls)[0]
        boxes = xyxy[idx]
        scores = confidence[idx]
        order = scores.argsort()[::-1]
        idx, boxes = idx[order], boxes[order]
        iou = _iou_matrix(boxes)
        suppressed = np.zeros(len(boxes), dtype=bool)
        for i in range(len(boxes)):
            if suppressed[i]:
                continue
            keep.append(idx[i])
            suppressed |= iou[i] > iou_thresh
    keep = sorted(keep)
    return xyxy[keep], confidence[keep], class_id[keep]


class RFDETR_TRT:
    def __init__(self, engine_path, apply_nms=True, nms_iou=0.7):
        logger = trt.Logger(trt.Logger.WARNING)
        with open(engine_path, "rb") as f, trt.Runtime(logger) as runtime:
            self.engine = runtime.deserialize_cuda_engine(f.read())
        self.context = self.engine.create_execution_context()

        self.input_name = None
        self.output_names = []
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            mode = self.engine.get_tensor_mode(name)
            if mode == trt.TensorIOMode.INPUT:
                self.input_name = name
            else:
                self.output_names.append(name)

        self.stream = torch.cuda.Stream()
        self.apply_nms = apply_nms
        self.nms_iou = nms_iou
        # TensorRT execution contexts are NOT thread-safe for concurrent
        # execute calls on the same context -- serialize here so this class
        # is safe even if the caller forgets an external lock.
        self._lock = threading.Lock()

    def _infer(self, inp_np):
        t = torch.from_numpy(inp_np).cuda()
        with self._lock:
            self.context.set_input_shape(self.input_name, tuple(t.shape))
            self.context.set_tensor_address(self.input_name, t.data_ptr())
            out_tensors = {}
            for name in self.output_names:
                shape = tuple(self.context.get_tensor_shape(name))
                trt_dtype = self.engine.get_tensor_dtype(name)
                torch_dtype = torch.float16 if trt_dtype == trt.DataType.HALF else torch.float32
                o = torch.empty(shape, dtype=torch_dtype, device="cuda")
                out_tensors[name] = o
                self.context.set_tensor_address(name, o.data_ptr())
            with torch.cuda.stream(self.stream):
                self.context.execute_async_v3(stream_handle=self.stream.cuda_stream)
            self.stream.synchronize()
        return {k: v.float().cpu().numpy() for k, v in out_tensors.items()}

    def predict(self, frame_rgb, threshold=0.25):
        """frame_rgb: HxWx3 RGB uint8 numpy array (same contract as
        RFDETRBase.predict which your pipeline calls with a cvtColor'd RGB
        frame). Returns Detections with xyxy in frame_rgb's own coordinate
        space -- no extra rescaling needed downstream."""
        h, w = frame_rgb.shape[:2]
        img = cv2.resize(frame_rgb, (RF_INPUT_SIZE, RF_INPUT_SIZE)).astype(np.float32) / 255.0
        img = (img - RF_MEAN) / RF_STD
        inp = np.ascontiguousarray(img.transpose(2, 0, 1)[None, ...], dtype=np.float32)

        try:
            outputs = self._infer(inp)
        except Exception:
            # Never let a single bad inference kill the 24/7 camera thread -- but
            # DO say so. Returning empty Detections here is indistinguishable
            # downstream from "nothing in frame", so without this line detection
            # can be dead on every camera, forever, in total silence.
            _THROTTLE.exception("rfdetr.infer",
                                "[RFDETR] TensorRT inference FAILED -- returning EMPTY "
                                "detections (detection is effectively OFF while this repeats)")
            return Detections(np.zeros((0, 4)), np.zeros((0,)), np.zeros((0,), dtype=int))

        dets_raw = outputs.get("dets")
        labels_raw = outputs.get("labels")
        if dets_raw is None or labels_raw is None:
            # No active exception here -- .error, not .exception, or the record
            # would carry a useless "NoneType: None" traceback.
            _THROTTLE.error("rfdetr.outputs",
                            "[RFDETR] engine returned no 'dets'/'labels' tensor "
                            "(got %s) -- returning EMPTY detections. Stale or "
                            "mismatched .engine file?", sorted(outputs.keys()))
            return Detections(np.zeros((0, 4)), np.zeros((0,)), np.zeros((0,), dtype=int))

        cls_scores_all = _sigmoid(labels_raw[0][:, :NUM_CLASSES])
        cls_ids = cls_scores_all.argmax(axis=1)
        cls_conf = cls_scores_all[np.arange(len(cls_ids)), cls_ids]

        keep_mask = cls_conf > threshold
        if not np.any(keep_mask):
            return Detections(np.zeros((0, 4)), np.zeros((0,)), np.zeros((0,), dtype=int))

        boxes_cxcywh = dets_raw[0][keep_mask]
        cls_ids = cls_ids[keep_mask]
        cls_conf = cls_conf[keep_mask]

        cx, cy, bw, bh = (boxes_cxcywh[:, 0], boxes_cxcywh[:, 1],
                           boxes_cxcywh[:, 2], boxes_cxcywh[:, 3])
        x1 = (cx - bw / 2) * w
        y1 = (cy - bh / 2) * h
        x2 = (cx + bw / 2) * w
        y2 = (cy + bh / 2) * h
        xyxy = np.stack([x1, y1, x2, y2], axis=1)
        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, w)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, h)

        if self.apply_nms:
            xyxy, cls_conf, cls_ids = class_aware_nms(xyxy, cls_conf, cls_ids, self.nms_iou)

        return Detections(xyxy, cls_conf, cls_ids.astype(int))