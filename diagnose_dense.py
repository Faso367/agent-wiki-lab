"""Separate diagnostic for exact cosine versus the ANN results in the measured index."""
import json
import numpy as np
from corpus import ROOT, STATE, questions
from embedding import Encoder
from retrieval import xhits


def main():
    bench = json.loads((STATE/"retrieval.json").read_text())
    index = bench["index"]
    cache = np.load(STATE/"cards.npz")
    ids, vectors = cache["ids"], cache["vectors"]
    encoder = Encoder()
    gold = {r["question_id"]:set(r["expected_doc_ids"]) for r in questions(set(ids))}
    results = []
    for row in (ROOT/"queries-ru.tsv").read_text().splitlines():
        key, text = row.split("\t",1)
        vec = encoder.encode([text],"query: ")[0]
        exact = ids[np.argsort(-(vectors@vec))[:5]].tolist()
        ann = xhits(index, {"knn":{"field":"vec","query_vector":vec.tolist(),"k":30}},30)[:5]
        results.append(dict(id=key,exact=exact,ann=ann,exact_hit=bool(gold[key]&set(exact)),
                            ann_hit=bool(gold[key]&set(ann)), overlap=len(set(exact)&set(ann))))
    (STATE/"dense.json").write_text(json.dumps(results,indent=2)+"\n")
    print("Exact hits, ANN hits, common top-5:", *(sum(r[k] for r in results)
          for k in ["exact_hit","ann_hit","overlap"]))


if __name__ == "__main__":
    main()
