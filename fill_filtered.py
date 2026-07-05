"""Targeted fill for content-filter-blocked blanks using a literary-framing
single-paragraph prompt. Updates the checkpoint in place."""
import sys, json, os, time; sys.path.insert(0, '.')
import config, translate
from concurrent.futures import ThreadPoolExecutor, as_completed

ck_path = os.path.join(config.CHECKPOINT_DIR, 'wagahai.json')
ck = json.load(open(ck_path))

blanks = []
for ci, c in enumerate(ck['chapters']):
    for j, p in enumerate(c['pairs']):
        if len(p) >= 2 and not p[1].strip():
            blanks.append((ci, j, p[0]))
print(f"blanks to fill: {len(blanks)}", file=sys.stderr)


def fill_one(args):
    ci, j, txt = args
    sys_prompt = (
        "You are translating '吾輩は猫である' (I Am a Cat, 1905) by Natsume "
        "Soseki — a public-domain classic of Japanese literature studied in "
        "schools. Translate the given Japanese sentence into natural Korean. "
        "This is a satirical novel; render literary content faithfully. "
        "Output ONLY the Korean translation, no commentary."
    )
    try:
        out = translate._chat([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Translate to Korean:\n{txt}"},
        ], timeout=60)
        return (ci, j, out.strip().replace(translate.PARA_DELIM, "").strip())
    except Exception as e:
        sys.stderr.write(f"  ch{ci}@{j} still blocked: {type(e).__name__}\n")
        return (ci, j, "")


t0 = time.time()
results = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(fill_one, b): b for b in blanks}
    done = 0
    for fut in as_completed(futs):
        ci, j, ko = fut.result()
        if ko:
            results[(ci, j)] = ko
        done += 1
        if done % 10 == 0:
            sys.stderr.write(f"  {done}/{len(blanks)} tried, "
                             f"{len(results)} filled\n")
print(f"filled {len(results)}/{len(blanks)} in {time.time()-t0:.0f}s",
      file=sys.stderr)

for (ci, j), ko in results.items():
    ck['chapters'][ci]['pairs'][j][1] = ko
json.dump(ck, open(ck_path, 'w'), ensure_ascii=False)

d = sum(1 for c in ck['chapters'] for p in c['pairs']
        if len(p) >= 2 and p[1].strip())
b = sum(1 for c in ck['chapters'] for p in c['pairs']
        if len(p) >= 2 and not p[1].strip())
print(f"FINAL: {d}/{d+b} ({100*d/(d+b):.1f}%), {b} blanks remain",
      file=sys.stderr)
