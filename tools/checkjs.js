// 语法体检：解析 demo HTML 里的 <script> 块（只解析不执行）
const fs = require('fs');
const path = process.argv[2] || 'BetterRAG-demo.html';
const html = fs.readFileSync(path, 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
console.log(`file: ${path}  (${(html.length / 1024).toFixed(0)} KB)`);
console.log(`script blocks: ${blocks.length}`);
let bad = 0;
blocks.forEach((b, i) => {
  try { new Function(b[1]); console.log(`  block ${i}: OK  (${b[1].length} chars)`); }
  catch (e) { bad++; console.log(`  block ${i}: SYNTAX ERROR -> ${e.message}`); }
});
// 结构体检
const need = ['id="map"', 'id="slider"', 'id="ov"', 'id="left"', 'id="right"', 'id="shots"'];
need.forEach(n => { if (!html.includes(n)) { console.log(`  MISSING: ${n}`); bad++; } });
console.log(bad ? `FAILED (${bad})` : 'ALL OK');
process.exit(bad ? 1 : 0);
