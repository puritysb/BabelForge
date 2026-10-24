"""Publish completed reviewed editions; remove obsolete copies after verification."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import catalog
import config
from finish_library_review import quotation_losses, abbreviation_boundary_repetitions
from quality_audit import _semantic_rows, _semantic_signature, find_translation_integrity_violations
from review_library import (VERSION, deserialize, digest, path_of, save,
                            load_review_sheet, checked_baseline)
from publication import publish_revision, PublicationError


def checked_review(root, entry):
    directory = root / entry['id']
    result = json.loads((directory/'result.json').read_text())
    state = json.loads((directory/'checkpoint.json').read_text())
    baseline = checked_baseline(directory, state)
    sheet = load_review_sheet(directory)
    book = deserialize(state['book'])
    profile = {'model':config.ZAI_MODEL,'quality_profile':sheet['profile_hash'],
               'source_sha256':baseline['source_sha256'],'review_version':VERSION}
    if state['stage'] != 'completed' or not result['quality_report']['passed']:
        raise PublicationError('Incomplete editorial review: '+entry['id'])
    if digest(path_of(entry['source_path']).read_bytes()) != baseline['source_sha256']:
        raise PublicationError('Retained source changed')
    verified = set(state['verified'])
    if (find_translation_integrity_violations(book) or abbreviation_boundary_repetitions(book) or
            not all(_semantic_signature(r,r['current_ko']) in verified
                    for r in _semantic_rows(book,None,profile))):
        raise PublicationError('Exact final-text approvals are incomplete')
    decisions_path = directory/'quotation-review.json'
    decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else []
    approved = {(r.get('chapter'),r.get('paragraph'),r.get('final_ko_sha256'))
                for r in decisions if r.get('decision')=='valid-source-grounded-style' and r.get('reason','').strip()}
    if any((f['chapter'],f['paragraph'],f['final_ko_sha256']) not in approved
           for f in quotation_losses(book,baseline)):
        raise PublicationError('Unresolved quotation boundaries')
    path = Path(result['output'])
    if digest(path.read_bytes()) != result['sha256']:
        raise PublicationError('Reviewed artifact changed')
    # Compare against the approved checkpoint, not a self-consistent altered EPUB.
    from epub_validate import validate_epub
    report = validate_epub(str(path), expected_content=[p for c in book.chapters for p in c.pairs])
    if report['epubcheck'] != 'passed':
        raise PublicationError('Official EPUBCheck required')
    return result, path


def publication_options(entry):
    options = dict(entry.get('publication') or {})
    destination = path_of(options.get('destination') or entry['inputs'][0])
    matches = [e for e in catalog._load()['entries'] if e.get('epub_path') and
               Path(e['epub_path']).resolve() == destination.resolve() and
               e.get('selected') and (e.get('status') in {'published','completed'} or
                                     e.get('publication_stage') == 'cleanup_pending')]
    if matches:
        latest = max(matches,key=lambda e:e.get('updated_ms',0))
        selected = latest['selected']
        options.setdefault('source',selected['source'])
        options.setdefault('source_id',selected['source_id'])
        options.setdefault('request_id',latest['id'])
    if not options.get('source_id'):
        url = urlsplit(entry['source_url'])
        parts = url.path.strip('/').split('/')
        if url.hostname in {'www.gutenberg.org','gutenberg.org'} and len(parts)==2 and parts[0]=='ebooks' and parts[1].isdigit():
            options.setdefault('source','gutenberg')
            options['source_id']=parts[1]
        else:
            raise PublicationError('Manifest publication.source/source_id required: '+entry['id'])
    options['destination']=str(destination)
    if not isinstance(options.get('source'), str) or not options['source']:
        raise PublicationError('Manifest publication.source required: '+entry['id'])
    return options


def run(manifest_path, check_only=False):
    manifest=json.loads(Path(manifest_path).read_text())
    root=path_of(manifest['review_dir'])
    # Fail on any unfinished edition or ambiguous identity before changing a library.
    prepared=[]
    for entry in manifest['editions']:
        result,path=checked_review(root,entry)
        options=publication_options(entry)
        prepared.append((entry,result,path,options))
    if check_only:
        return [{'id':e['id'],'path':str(p),'options':o} for e,r,p,o in prepared]
    results=[]
    for entry,result,path,options in prepared:
        # Expected old hashes were frozen when the edition was prepared, not
        # inferred from whatever happens to occupy the filename today.
        obsolete={str(path_of(raw)):h for raw,h in result['inputs'].items()
                  if path_of(raw).resolve()!=Path(options['destination']).resolve()}
        if not options.get('request_id'):
            options['request_id']=catalog.new_request(entry['title'])
            catalog.select(options['request_id'],{'source':options['source'],
                'source_id':options['source_id'],'title':entry['title'],'author':entry['author'],
                'language':entry['language'],'rights':entry['rights'],'fetch_url':entry['source_url']})
        title=options.pop('title',entry['title']+' (KO bilingual, 교정본)')
        state=publish_revision(str(path),title,entry['author'],rights=entry['rights'],
             source_lang=entry['language'],obsolete_files=obsolete,
             expected_sha256=result['sha256'],**options)
        results.append({'id':entry['id'],**state})
        save(root/'publication-results.json',results)
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    print(json.dumps(run(args.manifest,args.check_only),ensure_ascii=False,indent=2))
