"""
clip_trt.py

TensorRT replacement for the IMAGE half of your MobileCLIP verification
stage. Text-side stays in PyTorch exactly as you have it -- text features
are computed once at startup (cheap, one-time cost), so there's no benefit
to converting that half to TensorRT. Only the per-crop image encode, which
runs on every candidate detection, gets the TRT speedup.

Preprocessing note: your compare_pt_vs_trt_fullvideo.py exported/validated
the image engine using plain cv2.resize -> RGB -> /255 (see preprocess_clip
there), NOT open_clip's PIL-based resize/center-crop transform used in your
live pipeline's _clip_score(). Those two preprocessing paths do not
necessarily produce identical tensors. This wrapper reproduces the
cv2-based preprocessing your engine was actually built/tested against, so
swap the *entire* image-encode call in _clip_score / _clip_fallback to use
this -- don't mix TRT inference with the old open_clip preprocessing.

Usage:
    clip_img = CLIPImageEncoderTRT(CLIP_ENGINE_PATH, input_size=256)
    img_feat = clip_img.encode_image(crop_bgr)   # numpy (1, D), L2-normalized
    text_feats_np = cache["feats"].detach().cpu().numpy()  # precompute once
    sim = 100.0 * img_feat @ text_feats_np.T
"""

import threading
import numpy as np
import cv2
import torch
import tensorrt as trt

CLIP_MEAN = np.array([0.0, 0.0, 0.0], dtype=np.float32)
CLIP_STD = np.array([1.0, 1.0, 1.0], dtype=np.float32)


class CLIPImageEncoderTRT:
    def __init__(self, engine_path, input_size=256):
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
        self.input_size = input_size
        self._lock = threading.Lock()

    def _preprocess(self, crop_bgr):
        img = cv2.resize(crop_bgr, (self.input_size, self.input_size))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        img = (img - CLIP_MEAN) / CLIP_STD
        return np.ascontiguousarray(img.transpose(2, 0, 1)[None, ...], dtype=np.float32)

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

    def encode_image(self, crop_bgr):
        """crop_bgr: HxWx3 BGR uint8 numpy (straight from cv2, same as your
        CLAHE'd crop). Returns L2-normalized (1, D) numpy feature vector."""
        inp = self._preprocess(crop_bgr)
        outputs = self._infer(inp)
        feat = list(outputs.values())[0]
        norm = np.linalg.norm(feat, axis=-1, keepdims=True)
        norm = np.where(norm == 0, 1e-8, norm)
        return feat / norm