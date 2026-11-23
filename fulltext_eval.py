"""Measure serving path (top five full-text sections) independently from discovery cards."""
import json
import time
from statistics import median
from corpus import STATE, ROOT, questions
from wiki import DEFAULT_DB, connect, search


def main():
    ru = dict(l.split("\t",1) for l in (ROOT/"queries-ru.tsv").read_text().splitlines())
    with connect(DEFAULT_DB) as db:
        ids = {r[0] for r in db.execute("SELECT id FROM pages WHERE id GLOB 'dsid_*'")}
        # Measure the live wiki including any synthetic demo distractors.
        if db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] != len(ids):
            print("Live wiki includes demo pages among distractors.")
        output = []
        for q in questions(ids):
            key = q["question_id"]
            variants = {"en":q["question"]}
            if key in ru:
                variants["ru"]=ru[key]
            for language,text in variants.items():
                start = time.perf_counter()
                hits = search(db,text,5)
                ms=(time.perf_counter()-start)*1000
                output.append(dict(id=key,lang=language,hits=[r["url"] for r in hits],
                                   hit=bool({r["doc"] for r in hits}&set(q["expected_doc_ids"])),
                                   ms=round(ms,3)))
    (STATE/"fulltext.json").write_text(json.dumps(output,indent=2)+"\n")
    for lang in ["en","ru"]:
        subset = [r for r in output if r["lang"]==lang]
        print(lang,sum(r["hit"] for r in subset),len(subset),round(median(r["ms"] for r in subset),2))


if __name__=="__main__":
    main()
