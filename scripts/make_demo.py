"""Create a small bilingual EPUB demonstration without model or service calls."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ebooklib import epub
from assemble import assemble
from epub_markup import plain_text
from extract import _extract_epub
import config


def main():
    source = epub.EpubBook()
    source.set_identifier('babelforge-portable-demo')
    source.set_title('A River and a Promise')
    source.set_language('en')
    source.add_author('BabelForge')
    chapter = epub.EpubHtml(uid='chapter', file_name='text/chapter.xhtml', title='A River and a Promise')
    chapter.content = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
    <h1>A River and a Promise</h1>
    <p id="opening">The river was <em>quiet</em>, but the village was awake.<a href="notes.xhtml#note-1" epub:type="noteref">1</a></p>
    <figure><img src="../images/river.svg" alt="A river between two hills"/></figure>
    <h2>A short verse</h2><div class="verse"><p>One leaf falls.<br/>The water carries it home.</p></div>
    <h2>Words in context</h2><table><tbody><tr><th>Word</th><th>Meaning</th></tr><tr><td>Promise</td><td>A commitment to another person.</td></tr></tbody></table>
    <p>“I will return,” she said.</p>
    </body></html>'''
    notes = epub.EpubHtml(uid='notes', file_name='text/notes.xhtml', title='Notes')
    notes.content = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
    <h1>Notes</h1><aside id="note-1" epub:type="footnote"><p>This is an original demonstration passage. <a href="chapter.xhtml#opening">Return</a></p></aside>
    </body></html>'''
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="200" viewBox="0 0 600 200"><rect width="600" height="200" fill="#f2eee3"/><path d="M0 160 L130 40 L280 160 M320 160 L460 30 L600 160" fill="#b1c5a8"/><path d="M260 0 Q400 60 300 110 T280 200" fill="none" stroke="#588fa5" stroke-width="40"/></svg>'
    source.add_item(chapter)
    source.add_item(notes)
    source.add_item(epub.EpubItem(uid='river', file_name='images/river.svg', media_type='image/svg+xml', content=svg))
    source.add_item(epub.EpubNav())
    source.spine = [chapter]
    source.toc = [chapter]
    translations = {
        'A River and a Promise': '강과 약속',
        'The river was quiet, but the village was awake.1': '강은 <em>고요했지만</em>, 마을은 깨어 있었다.<a href="chapter2.xhtml#note-1" epub:type="noteref">1</a>',
        'A short verse': '짧은 시',
        'One leaf falls. The water carries it home.': '잎 하나가 떨어진다.<br/>물은 그 잎을 집으로 데려간다.',
        'Words in context': '문맥 속의 낱말',
        'Word': '낱말', 'Meaning': '의미', 'Promise': '약속',
        'A commitment to another person.': '다른 사람에게 지키겠다고 하는 다짐.',
        '“I will return,” she said.': '“돌아올게요.” 그녀가 말했다.',
        'Notes': '주석',
        'This is an original demonstration passage. Return': '이 글은 기능을 보여 주기 위해 새로 작성한 예문이다. <a href="chapter1.xhtml#opening">돌아가기</a>',
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / 'source.epub'
        epub.write_epub(str(path), source)
        chapters = _extract_epub(str(path))
        result = {'title': 'A River and a Promise', 'author': 'BabelForge',
                  'chapter_titles_ko': ['강과 약속', '주석'], 'chapters': []}
        for ch in chapters:
            result['chapters'].append({
                'title': ch.title,
                'pairs': [(text, translations[plain_text(text)]) for text in ch.paragraphs],
                'template': ch.template, 'assets': ch.assets,
            })
        output = Path(config.DATA_DIR) / 'examples' / 'portable-bilingual-demo.epub'
        report = {}
        assemble(result, str(output), validation_report=report)
    print(output)
    print(report)


if __name__ == '__main__':
    main()
