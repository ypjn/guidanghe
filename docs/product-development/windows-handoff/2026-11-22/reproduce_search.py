"""Read-only product source probe; all files/databases live in a system temporary directory by default.

Run with the project's existing Python, importing the current worktree source.
No product source, test file, or real user data is modified.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import argparse

parser = argparse.ArgumentParser(description='Portable search regression probe; source files are read-only.')
parser.add_argument('--source', type=Path, help='Product source directory; default: current checkout')
parser.add_argument('--output-dir', type=Path, help='Diagnostic output directory; default: system temporary directory')
parser.add_argument('--expect-known-bugs', action='store_true', help='Diagnostic mode: success means all three old defects reproduce')
args = parser.parse_args()
REPO = Path(__file__).resolve().parents[4]
SRC = (args.source or (REPO / '归档盒源码')).resolve()
if not (SRC / 'search_session.py').is_file():
    parser.error('Cannot locate product source; pass --source explicitly')
OUT = args.output_dir or Path(tempfile.mkdtemp(prefix='guidanghe-search-review-'))
OUT.mkdir(parents=True, exist_ok=True)
DATA = Path(tempfile.mkdtemp(prefix='fixture-', dir=OUT))
os.environ['ARCHIVEBOX_APP_DIR'] = str(DATA / 'appdata')
os.environ['ARCHIVEBOX_INDEX_DB'] = str(DATA / 'default-index.db')
os.environ['ARCHIVEBOX_RECENT_FILE'] = str(DATA / 'recent.json')
sys.path.insert(0, str(SRC))

import content_search
import search_session
from index_service import update_space
from index_store import IndexStore, SearchQuery, SpaceSpec
from search_intent import understand


def new_case(label):
    base = DATA / label
    root = base / 'source'
    root.mkdir(parents=True)
    store = IndexStore(str(base / 'index.db'))
    spec = SpaceSpec('sp-' + label, str(root), label, {})
    store.upsert_space(spec, '2026-11-22T00:00:00')
    return root, store, spec


def write(root, rel, text='neutral'):
    (root / rel).write_text(text, encoding='utf-8')


def refresh(store, spec, content=False):
    report = update_space(spec.space_id, store=store, read_content=content)
    assert report.completed and not report.errors, report


def ids(page):
    return {hit.file_id for hit in page.hits}


results = {
    'python': sys.executable,
    'source_imports': [content_search.__file__, search_session.__file__],
    'head': subprocess.check_output(
        ['git', '-C', str(SRC.parent), 'rev-parse', 'HEAD'], text=True).strip(),
    'fixture': str(DATA),
}

# Case 1: all 75 files match both words. The second page must contain 25
# different records. All are metadata-only so no body/name duplicate can
# explain the observed overlap.
root, store, spec = new_case('multi-pagination')
try:
    for n in range(75):
        write(root, f'alpha_beta_{n:03d}.bin')
    refresh(store, spec)
    request = understand('alpha beta')
    pages = [search_session.run_lookup(store, request, limit=50, offset=offset)
             for offset in (0, 50, 100)]
    raw = content_search.search_files(store, SearchQuery(text='alpha', limit=200))
    case = {
        'source_file_count': len(store.present_files()),
        'alpha_match_count': len(raw.hits),
        'expected_page_counts': [50, 25, 0],
        'actual_page_counts': [len(page.hits) for page in pages],
        'page_1_page_2_id_overlap': len(ids(pages[0]) & ids(pages[1])),
        'all_3_pages_identical': ids(pages[0]) == ids(pages[1]) == ids(pages[2]),
    }
    case['reproduced'] = case['actual_page_counts'] == [50, 50, 50] and case['all_3_pages_identical']
    case['normal_behavior_passed'] = case['actual_page_counts'] == [50, 25, 0] and not (ids(pages[0]) & ids(pages[1])) and len(set.union(*(ids(page) for page in pages))) == 75
    results['multi_token_offset_ignored'] = case
finally:
    store.close()

# Case 2: alpha is in all 80 file names; beta occurs only in the body of
# alpha's last-sorted file. The actual complete intersection has one file,
# but the first 50 alpha hits exclude that file before run_lookup intersects.
root, store, spec = new_case('multi-intersection')
try:
    for n in range(80):
        write(root, f'alpha_{n:03d}.txt')
    refresh(store, spec)
    target = max(store.present_files(), key=lambda item: item.file_id)
    write(root, target.rel_path, 'beta')
    refresh(store, spec, content=True)
    alpha = content_search.search_files(store, SearchQuery(text='alpha', limit=200))
    beta = content_search.search_files(store, SearchQuery(text='beta', limit=200))
    actual = search_session.run_lookup(store, understand('alpha beta'), limit=50)
    common = ids(alpha) & ids(beta)
    case = {
        'alpha_match_count': len(alpha.hits),
        'beta_match_count': len(beta.hits),
        'complete_intersection_count': len(common),
        'target_path': target.rel_path,
        'target_id': target.file_id,
        'actual_lookup_count': len(actual.hits),
        'target_is_last_alpha_hit': alpha.hits[-1].file_id == target.file_id,
    }
    case['reproduced'] = len(common) == 1 and not actual.hits and case['target_is_last_alpha_hit']
    case['normal_behavior_passed'] = len(common) == 1 and ids(actual) == common
    results['multi_token_intersection_after_truncation'] = case
finally:
    store.close()

# Case 3: use deterministic input names to place 60 excluded draft files
# before one valid keep file in the actual store's file_id ordering. Input
# selection does not mutate the DB or mock any product search call.
root, store, spec = new_case('exclude-pagination')
try:
    def planned_id(rel):
        return hashlib.sha256(f'{spec.space_id}\n{rel.casefold()}'.encode()).hexdigest()[:32]

    candidates = [f'gamma_draft_{n:04d}.bin' for n in range(2000)]
    drafts = [rel for rel in candidates if planned_id(rel) < '8'][:60]
    keep = next(f'gamma_keep_{n:04d}.bin' for n in range(2000)
                if planned_id(f'gamma_keep_{n:04d}.bin') > 'f')
    assert len(drafts) == 60
    for rel in drafts + [keep]:
        write(root, rel)
    refresh(store, spec)
    raw = content_search.search_files(store, SearchQuery(text='gamma', limit=200))
    request = understand('gamma，排除draft')
    page1 = search_session.run_lookup(store, request, limit=50, offset=0)
    page2 = search_session.run_lookup(store, request, limit=50, offset=50)
    expected = [hit for hit in raw.hits if 'draft' not in hit.rel_path]
    case = {
        'query': request.text,
        'excluded': request.excluded,
        'raw_match_count': len(raw.hits),
        'full_filter_valid_paths': [hit.rel_path for hit in expected],
        'raw_first_50_all_excluded': all('draft' in hit.rel_path for hit in raw.hits[:50]),
        'actual_first_page_count': len(page1.hits),
        'actual_second_page_paths': [hit.rel_path for hit in page2.hits],
        'ui_next_page_guard': 'search_dialog._next_page returns without advancing when self.hits is empty',
    }
    case['reproduced'] = len(expected) == 1 and not page1.hits and len(page2.hits) == 1
    case['normal_behavior_passed'] = len(expected) == 1 and ids(page1) == {hit.file_id for hit in expected} and not page2.hits
    results['exclusion_after_pagination_empty_first_page'] = case
finally:
    store.close()

results['all_three_reproduced'] = all(
    results[key]['reproduced'] for key in (
        'multi_token_offset_ignored',
        'multi_token_intersection_after_truncation',
        'exclusion_after_pagination_empty_first_page'))
results['all_expected_behaviors_passed'] = all(
    results[key]['normal_behavior_passed'] for key in (
        'multi_token_offset_ignored',
        'multi_token_intersection_after_truncation',
        'exclusion_after_pagination_empty_first_page'))
results['mode'] = 'expect-known-bugs' if args.expect_known_bugs else 'normal-regression'
destination = OUT / 'results.json'
destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
print(json.dumps(results, ensure_ascii=False, indent=2))
passed = results['all_three_reproduced'] if args.expect_known_bugs else results['all_expected_behaviors_passed']
sys.exit(0 if passed else 1)
