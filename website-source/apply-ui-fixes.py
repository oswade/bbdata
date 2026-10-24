#!/usr/bin/env python3
"""Idempotent patch of the full Lovable-derived BillBot source in the CI build."""
from pathlib import Path
import re
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else 'website-build')
src = root / 'src'

def update(file, fn):
    p = src / file
    old = p.read_text()
    new = fn(old)
    if old == new:
        raise SystemExit(f'Expected change not found in {p}')
    p.write_text(new)

clipboard = r'''/** Android file:// clipboard fallback: run selection copy inside click activation. */
function selectionCopy(text: string): boolean {
  if (typeof document === 'undefined' || typeof document.execCommand !== 'function') return false;
  const input = document.createElement('textarea');
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  input.value = text;
  input.readOnly = true;
  input.setAttribute('aria-hidden', 'true');
  input.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;z-index:-1;font-size:16px';
  let ok = false;
  try {
    document.body.appendChild(input);
    input.focus({ preventScroll:true });
    input.select();
    input.setSelectionRange(0,text.length);
    ok = document.execCommand('copy');
  } catch { ok = false; }
  finally {
    input.remove();
    try { previous?.focus({ preventScroll:true }); } catch { /* noop */ }
  }
  return ok;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;
  // Local Android HTML may have no navigator.clipboard. This MUST run before awaiting.
  if (typeof window !== 'undefined' && (window.location.protocol === 'file:' || !window.isSecureContext))
    if (selectionCopy(text)) return true;
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return true; }
    catch { /* fallback below */ }
  }
  return selectionCopy(text);
}
'''
(src/'lib/billbot/clipboard.ts').write_text(clipboard)

def ai(s):
    s=s.replace("import { useMemo, useRef, useState } from 'react';",
       "import { useMemo, useRef, useState } from 'react';\nimport { copyToClipboard } from '@/lib/billbot/clipboard';")
    start=s.index('  async function copy(text:string, context:string) {')
    end=s.index('\n  async function share()',start)
    return s[:start] + '''  async function copy(text:string, context:string) {
    if (await copyToClipboard(text)) {
      setNotice(context + ' copied to clipboard. Paste it into ' + selected.name + '.');
      return;
    }
    if (context === 'Task prompt') { promptElement.current?.focus(); promptElement.current?.select(); }
    setNotice('Clipboard access blocked in this browser. Long-press the text and choose Copy, or use HTTPS.');
  }
''' + s[end:]
update('routes/ai.tsx',ai)

def nbn(s):
    s=s.replace('import { Input } from "@/components/ui/input";\n','')
    s=s.replace('queryKey: ["nbn", p.nbnTier, p.nbnCurrentMonthly], queryFn: () => compareNbn({ data: { tier: p.nbnTier, current_monthly: p.nbnCurrentMonthly } })',
                'queryKey: ["nbn", p.nbnTier], queryFn: () => compareNbn({ data: { tier: p.nbnTier, current_monthly: null } })')
    start=s.index('        <section>\n          <label className="text-sm font-semibold" htmlFor="nbncur">')
    end=s.index('        {q.isLoading',start)
    return s[:start]+s[end:]
update('routes/nbn.tsx',nbn)
update('styles.css',lambda s:s.replace(
    'height: clamp(240px, calc(100dvh - var(--results-top, 300px) - 92px), 760px);',
    'height: var(--results-available-height, calc(100dvh - 240px));'))

def columns(s):
    s=re.sub(r'if \(list\) element\.style\.setProperty\("--results-top",.*?;',
      """if (list) {
        const nav = document.querySelector<HTMLElement>('nav.fixed');
        const bottom = nav?.getBoundingClientRect().top ?? window.innerHeight;
        // Viewport coordinates: adding scrollY previously shortened columns as page scrolled.
        const height = Math.max(120, Math.floor(bottom - list.getBoundingClientRect().top - 8));
        element.style.setProperty('--results-available-height', String(height) + 'px');
      }""",s,count=1)
    s=s.replace('if (element.parentElement) observer.observe(element.parentElement);',
      "const nav = document.querySelector('nav.fixed');\n    if (nav) observer.observe(nav);")
    s=s.replace('window.addEventListener("resize", measure);',
      'window.addEventListener("resize", measure);\n    window.addEventListener("scroll", measure, { passive:true });\n    window.visualViewport?.addEventListener("resize", measure);')
    s=s.replace('window.removeEventListener("resize", measure);',
      'window.removeEventListener("resize", measure);\n      window.removeEventListener("scroll", measure);\n      window.visualViewport?.removeEventListener("resize", measure);')
    return s
update('components/billbot/ComparisonColumns.tsx',columns)

test = r'''import { describe, expect, it, vi, afterEach } from 'vitest';
import { copyToClipboard } from './clipboard';

describe('LLM copy on local HTML', () => {
  afterEach(() => vi.restoreAllMocks());
  it('uses a selection-based clipboard fallback', async () => {
    const fn=vi.fn().mockReturnValue(true);
    Object.defineProperty(document,'execCommand',{configurable:true,value:fn});
    expect(await copyToClipboard('task text')).toBe(true);
    expect(fn).toHaveBeenCalledWith('copy');
    expect(document.querySelector('textarea[aria-hidden="true"]')).toBeNull();
  });
  it('never reports copied when all copy methods fail', async () => {
    Object.defineProperty(document,'execCommand',{configurable:true,value:vi.fn().mockReturnValue(false)});
    expect(await copyToClipboard('task text')).toBe(false);
  });
});
'''
(src/'lib/billbot/clipboard.test.ts').write_text(test)

assert 'nbncur' not in (src/'routes/nbn.tsx').read_text()
assert '--results-available-height' in (src/'components/billbot/ComparisonColumns.tsx').read_text()
assert 'clipboard' in (src/'routes/ai.tsx').read_text()
print('BillBot clipboard, NBN and column-height fixes applied')
