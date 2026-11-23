from pathlib import Path
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
from corpus import STATE, card, documents, verify_inputs


class Encoder:
    def __init__(self):
        models = STATE / "models"
        self.tok = Tokenizer.from_file(str(models / "tokenizer.json"))
        self.tok.enable_truncation(max_length=192)
        self.tok.enable_padding(pad_id=1, pad_token="<pad>")
        config = ort.SessionOptions()
        config.intra_op_num_threads = 4
        config.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(models / "model_int8.onnx"), config,
                                            providers=["CPUExecutionProvider"])

    def encode(self, texts, prefix):
        batch = self.tok.encode_batch([prefix + t for t in texts])
        ids = np.array([b.ids for b in batch], dtype=np.int64)
        mask = np.array([b.attention_mask for b in batch], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in {i.name for i in self.session.get_inputs()}:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]
        vectors = (hidden * mask[:, :, None]).sum(1) / mask.sum(1, keepdims=True)
        return (vectors / np.linalg.norm(vectors, axis=1, keepdims=True)).astype("float32")


def build():
    verify_inputs()
    encoder = Encoder()
    rows = [(doc, card(t, b)) for doc, t, b in documents()]
    vectors = []
    for start in range(0, len(rows), 16):
        batch = rows[start:start+16]
        vectors.append(encoder.encode([text for _, text in batch], "passage: "))
        if start % 512 == 0:
            print(f"{start}/{len(rows)}", flush=True)
    tmp = STATE / "cards.partial.npz"
    np.savez(tmp, ids=[doc for doc, _ in rows], vectors=np.vstack(vectors))
    tmp.replace(STATE / "cards.npz")


if __name__ == "__main__":
    build()
