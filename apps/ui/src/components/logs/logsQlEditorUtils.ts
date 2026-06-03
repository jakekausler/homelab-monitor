// Pure helpers for the single-line LogsQL editor.
//
// This module MUST stay free of @codemirror/* (or any heavy) imports so it can
// be unit-tested without pulling the CodeMirror chunk into the test's module
// graph. LogsQlEditorImpl imports sanitizeSingleLineInsert from here.

/**
 * Sanitize a single inserted text chunk for the single-line LogsQL editor.
 * - Pure newline insertion (bare Enter / OS line-break) → drop entirely ("").
 * - Newlines among content (multi-line paste) → collapse [\r\n]+ to a space.
 * - No newlines → unchanged.
 */
export function sanitizeSingleLineInsert(text: string): string {
  return /^[\r\n]+$/.test(text) ? '' : text.replace(/[\r\n]+/g, ' ')
}
