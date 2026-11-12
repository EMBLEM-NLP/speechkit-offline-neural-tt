// Tokenize JavaScript sources for the code-comment audit (emblem_rsi/code): node js_tokens.mjs < request.json
// request: [{"id": "...", "source": "...", "module": true|false}]
// reply:   {"<id>": {"ok": bool, "error": "...", "tokens": [[label, value, startLine, endLine, startCol, endCol]],
//                    "comments": [[startLine, endLine, block]]}}
// acorn is vendored in emblem_rsi/js/vendor, so no npm install is needed. tokenizer() yields tokens one at a time;
// parse() builds a full syntax tree and therefore catches every syntax error.
import { tokenizer, parse } from "../js/vendor/acorn.mjs";

const chunks = [];  // raw stdin buffers, collected until the stream ends
// top-level await (allowed in an ES module) reads the whole request from stdin
for await (const c of process.stdin) chunks.push(c);
// join the buffers, decode UTF-8 and parse the JSON request array
const req = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const out = {};  // reply object holding one result per request id
for (const r of req) {  // handle each source separately, so one failure does not spoil the batch
  const comments = [];  // comment ranges for this source, as [startLine, endLine, isBlock]
  const tokens = [];  // token records for this source, in source order
  // Parser options: the newest syntax acorn knows, module or classic script as requested, and line/column locations on
  // every token, which are needed to map tokens to lines.
  const opts = { ecmaVersion: "latest", sourceType: r.module ? "module" : "script", locations: true,
                 // accept a #! first line; classic scripts may also return at top level
                 allowHashBang: true, allowReturnOutsideFunction: !r.module,
                 // onComment records each comment's first and last line and its kind
                 onComment: (block, text, start, end, sl, el) => comments.push([sl.line, el.line, block]) };
  try {  // a syntax error from parse() or tokenizer() lands in the catch block
    // full parse first, since the tokenizer alone misses many syntax errors; comments are not recorded here
    parse(r.source, { ...opts, onComment: undefined });
    // then walk the token stream; onComment fires for comments along the way
    for (const t of tokenizer(r.source, opts)) {
      // Token record: acorn's label (for example 'name', 'string' or '{'), its value as text or null, then the start
      // and end line. A regular-expression token's value is an object {pattern, flags, value}, so it is written back as
      // /pattern/flags; String() alone would turn every regex into "[object Object]" and hide regex changes.
      const v = t.value === undefined ? null : t.type.label === "regexp" ? `/${t.value.pattern}/${t.value.flags}` :
        String(t.value);  // names, strings, numbers and template text as plain text
      tokens.push([t.type.label, v, t.loc.start.line, t.loc.end.line,  // label, value, first and last line
                   t.loc.start.column, t.loc.end.column]);  // start and end columns, counted from zero
    }
    out[r.id] = { ok: true, tokens, comments };  // success: the token list and the comment ranges
  } catch (e) {  // parsing failed for this particular source
    // keep the error message, with empty lists, so the caller marks the file unparsable
    out[r.id] = { ok: false, error: String(e && e.message || e), tokens: [], comments: [] };
  }
}
process.stdout.write(JSON.stringify(out));  // one compact JSON reply on stdout, read by langs.js_tokens
