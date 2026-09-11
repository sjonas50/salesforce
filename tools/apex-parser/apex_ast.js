#!/usr/bin/env node
/**
 * Apex AST emitter for Off-Ramp (AD-31 follow-up).
 *
 * Wraps Salesforce's ANTLR grammar (@apexdevtools/apex-parser) and emits a
 * compact JSON parse tree per request so the Python analyzer can walk it.
 *
 * Protocol (persistent server): one JSON request per stdin line,
 *   {"id": "...", "source": "...", "kind": "class" | "trigger" | "auto"}
 * one JSON response per stdout line,
 *   {"id": "...", "tree": [...], "errors": [{"line": n, "col": n, "msg": "..."}]}
 *
 * Tree encoding: rule nodes are arrays ["RuleName", child, child, ...]; terminals are
 * strings. Pure punctuation terminals are dropped (the rule name already carries the
 * meaning). SOQL/SOSL literals are emitted as ["SoqlLiteral", "<verbatim source>"]
 * so the query can be parsed on the Python side.
 *
 * `node apex_ast.js --version` prints the grammar package version and exits.
 */
'use strict';

const readline = require('node:readline');
const antlr4 = require('antlr4');
const { ApexParserFactory, ApexErrorListener } = require('@apexdevtools/apex-parser');

const DROP = new Set(['(', ')', '{', '}', ';', ',', '.', '<', '>', '<EOF>']);
const OPAQUE = new Set(['SoqlLiteral', 'SoslLiteral']);

class CollectingErrorListener extends ApexErrorListener {
  constructor() {
    super();
    this.errors = [];
  }
  apexSyntaxError(line, col, msg) {
    this.errors.push({ line, col, msg: String(msg).slice(0, 200) });
  }
}

function ruleName(ctx) {
  return ctx.constructor.name.replace(/Context$/, '');
}

function encode(ctx, source) {
  const name = ruleName(ctx);
  if (OPAQUE.has(name)) {
    const start = ctx.start ? ctx.start.start : 0;
    const stop = ctx.stop ? ctx.stop.stop : start;
    return [name, source.slice(start, stop + 1)];
  }
  const out = [name];
  const children = ctx.children || [];
  for (const child of children) {
    if (child.symbol !== undefined) {
      // terminal node
      const text = child.getText();
      if (!DROP.has(text)) out.push(text);
    } else {
      out.push(encode(child, source));
    }
  }
  return out;
}

const TRIGGER_RE = /^\s*trigger\s+[A-Za-z_][A-Za-z0-9_]*\s+on\b/i;

function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, ' ');
}

function parseOnce(source, isTrigger, mode) {
  const parser = ApexParserFactory.createParser(source);
  const listener = new CollectingErrorListener();
  parser.removeErrorListeners();
  parser.addErrorListener(listener);
  parser._interp.predictionMode = mode;
  const tree = isTrigger ? parser.triggerUnit() : parser.compilationUnit();
  return { tree, errors: listener.errors };
}

function parse(source, kind) {
  const isTrigger = kind === 'trigger' || (kind !== 'class' && TRIGGER_RE.test(stripComments(source)));
  // SLL first (fast); fall back to full LL only when SLL reports an error.
  let result = parseOnce(source, isTrigger, antlr4.PredictionMode.SLL);
  if (result.errors.length) {
    result = parseOnce(source, isTrigger, antlr4.PredictionMode.LL);
  }
  return { tree: encode(result.tree, source), errors: result.errors };
}

function main() {
  if (process.argv.includes('--version')) {
    const fs = require('node:fs');
    const path = require('node:path');
    const pkgPath = path.join(__dirname, 'node_modules', '@apexdevtools', 'apex-parser', 'package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
    process.stdout.write(JSON.stringify({ parser: pkg.version, node: process.version }) + '\n');
    return;
  }
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on('line', (line) => {
    if (!line.trim()) return;
    let req;
    try {
      req = JSON.parse(line);
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: null, error: 'bad request: ' + e.message }) + '\n');
      return;
    }
    try {
      const { tree, errors } = parse(req.source || '', req.kind || 'auto');
      process.stdout.write(JSON.stringify({ id: req.id, tree, errors }) + '\n');
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: req.id, error: String(e && e.message || e) }) + '\n');
    }
  });
}

main();
