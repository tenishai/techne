;; Python tree-sitter query patterns for Codex symbol + call extraction (techne#89).
;;
;; Adapted from the basemind project (github.com/Goldziher/basemind, MIT licence).
;; See NOTICE for attribution. Capture names remapped to match techne's
;; extraction layer; decorator and implementation patterns omitted (not used in v1).
;;
;; section: symbols
;;
;; function_definition matches BOTH bare functions and methods inside a class body.
;; The extraction layer in source.py distinguishes them via parent chain inspection:
;; a function whose parent is a 'block' nested under 'class_definition' is a method.

(function_definition name: (identifier) @symbol.name) @symbol.function

(class_definition name: (identifier) @symbol.name) @symbol.class

;; section: calls

(call function: (identifier) @call.callee) @call.range
(call function: (attribute attribute: (identifier) @call.callee)) @call.range

;; section: imports
;;
;; Both plain 'import X' and 'from X import Y' are captured as import.range.
;; The extraction layer walks the node children to pick out the imported names.

(import_statement) @import.range
(import_from_statement) @import.range
