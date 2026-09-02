/**
 * 교재 원고 마크다운 → Word(.docx) 변환기
 * 사용법: node md2docx.js <입력.md> <출력.docx>
 *
 * 지원 문법
 *   앞머리(--- part: / chapter: ---), ## 절, ### 항, 문단,
 *   - 글머리, 1. 번호, | 표 |, > 인용, [^n]: 주, **굵게**
 */
const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, convertInchesToTwip,
} = require('docx');

const SANS = 'Malgun Gothic';
const SERIF = 'Batang';
const TABLE_W = 9360;

// **굵게** 를 TextRun 배열로
function runs(text, base = {}) {
  const out = [];
  text.split(/(\*\*[^*]+\*\*)/).forEach((seg) => {
    if (!seg) return;
    const m = seg.match(/^\*\*([^*]+)\*\*$/);
    out.push(new TextRun({
      text: m ? m[1] : seg,
      font: base.font || SERIF,
      size: base.size || 21,
      bold: m ? true : base.bold,
      color: base.color,
    }));
  });
  return out.length ? out : [new TextRun({ text: '', font: base.font || SERIF, size: base.size || 21 })];
}

const para = (text) => new Paragraph({
  spacing: { after: 160, line: 340 },
  alignment: AlignmentType.JUSTIFIED,
  indent: { firstLine: convertInchesToTwip(0.13) },
  children: runs(text),
});

const h2 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_2,
  spacing: { before: 400, after: 160 },
  children: runs(text, { font: SANS, size: 24, bold: true, color: '2F5496' }),
});

const h3 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_3,
  spacing: { before: 260, after: 120 },
  children: runs(text, { font: SANS, size: 21, bold: true, color: '1F3864' }),
});

const bullet = (text) => new Paragraph({
  numbering: { reference: 'dash-list', level: 0 },
  spacing: { after: 90, line: 320 },
  children: runs(text, { size: 20 }),
});

const numbered = (label, text) => new Paragraph({
  spacing: { after: 90, line: 320 },
  indent: { left: convertInchesToTwip(0.34), hanging: convertInchesToTwip(0.24) },
  children: runs(`${label} ${text}`, { size: 20 }),
});

const quote = (text) => new Paragraph({
  spacing: { before: 80, after: 160, line: 320 },
  indent: { left: convertInchesToTwip(0.32), right: convertInchesToTwip(0.2) },
  border: { left: { style: BorderStyle.SINGLE, size: 12, color: 'B4C6E7', space: 10 } },
  children: runs(text, { size: 20 }),
});

const note = (text) => new Paragraph({
  spacing: { after: 100, line: 300 },
  indent: { left: convertInchesToTwip(0.42), hanging: convertInchesToTwip(0.42) },
  children: runs(text, { size: 18 }),
});

function makeTable(rows) {
  const n = rows[0].length;
  const widths = [];
  // 첫 열은 좁게, 나머지는 균등
  const first = n > 2 ? Math.round(TABLE_W * 0.18) : Math.round(TABLE_W * 0.22);
  widths.push(first);
  const rest = Math.floor((TABLE_W - first) / (n - 1));
  for (let i = 1; i < n; i += 1) widths.push(i === n - 1 ? TABLE_W - first - rest * (n - 2) : rest);

  const cell = (t, i, header) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    shading: header ? { type: ShadingType.CLEAR, fill: 'DCE6F1', color: 'auto' } : undefined,
    margins: { top: 90, bottom: 90, left: 100, right: 100 },
    children: [new Paragraph({
      spacing: { after: 0, line: 264 },
      alignment: header ? AlignmentType.CENTER : undefined,
      children: runs(t, { font: SANS, size: 17, bold: header || i === 0 }),
    })],
  });

  return new Table({
    columnWidths: widths,
    width: { size: TABLE_W, type: WidthType.DXA },
    rows: rows.map((r, ri) => new TableRow({
      tableHeader: ri === 0,
      children: r.map((t, i) => cell(t, i, ri === 0)),
    })),
  });
}

// ---------- 파싱 ----------
const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) { console.error('사용법: node md2docx.js <입력.md> <출력.docx>'); process.exit(1); }
const src = fs.readFileSync(inPath, 'utf8').split(/\r?\n/);

let part = '';
let chapter = '';
let i = 0;
if (src[0].trim() === '---') {
  i = 1;
  while (i < src.length && src[i].trim() !== '---') {
    const m = src[i].match(/^(part|chapter):\s*(.+)$/);
    if (m) { if (m[1] === 'part') part = m[2].trim(); else chapter = m[2].trim(); }
    i += 1;
  }
  i += 1;
}

const body = [];
if (part) {
  body.push(new Paragraph({ spacing: { before: 200, after: 60 },
    children: [new TextRun({ text: part, font: SANS, size: 20, color: '767171' })] }));
}
if (chapter) {
  body.push(new Paragraph({ spacing: { before: 0, after: 300 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: '1F3864', space: 8 } },
    children: [new TextRun({ text: chapter, font: SANS, size: 40, bold: true, color: '1F3864' })] }));
}

let tableBuf = null;
const flushTable = () => {
  if (!tableBuf) return;
  const rows = tableBuf.filter((r) => !/^\s*\|?[\s:|-]+\|?\s*$/.test(r.join('')) || r.some((c) => /[^\s:-]/.test(c)));
  if (rows.length) body.push(makeTable(rows));
  body.push(new Paragraph({ spacing: { after: 140 }, children: [new TextRun({ text: '' })] }));
  tableBuf = null;
};

for (; i < src.length; i += 1) {
  const raw = src[i];
  const line = raw.trim();

  if (line.startsWith('|')) {
    const cells = line.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue; // 구분선
    if (!tableBuf) tableBuf = [];
    tableBuf.push(cells);
    continue;
  }
  flushTable();

  if (!line) continue;
  if (/^---+$/.test(line)) continue;
  if (line.startsWith('# ')) continue; // 제목은 앞머리로 처리
  if (line.startsWith('### ')) { body.push(h3(line.slice(4))); continue; }
  if (line.startsWith('## ')) { body.push(h2(line.slice(3))); continue; }
  if (line.startsWith('> ')) { body.push(quote(line.slice(2))); continue; }
  if (/^\[\^[^\]]+\]:/.test(line)) {
    body.push(note(line.replace(/^\[\^([^\]]+)\]:\s*/, '[$1] ')));
    continue;
  }
  const num = line.match(/^(\d+\.)\s+(.*)$/);
  if (num) { body.push(numbered(num[1], num[2])); continue; }
  if (/^[-*]\s+/.test(line)) { body.push(bullet(line.replace(/^[-*]\s+/, ''))); continue; }
  body.push(para(line));
}
flushTable();

const doc = new Document({
  numbering: {
    config: [{
      reference: 'dash-list',
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: '–', alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: convertInchesToTwip(0.32), hanging: convertInchesToTwip(0.2) } } },
      }],
    }],
  },
  styles: { default: { document: { run: { font: SERIF, size: 21 } } } },
  sections: [{
    properties: { page: { margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
    children: body,
  }],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(outPath, buf); console.log('생성:', outPath); });
