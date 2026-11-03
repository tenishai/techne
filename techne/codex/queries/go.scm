;; Go tree-sitter query patterns for Codex symbol + call extraction (techne#1284).
;;
;; Capture names match the Python and TypeScript files, so `_parse_file` reads
;; all three the same way. A struct is kind `class` and an interface is kind
;; `interface`; other named types (`type ID string`, `type F func()`) and type
;; aliases are not symbols in v1.
;;
;; section: symbols

(function_declaration name: (identifier) @symbol.name) @symbol.function

;; func (s *Server) Serve() — the receiver is read by `_go_receiver_type`.
(method_declaration name: (field_identifier) @symbol.name) @symbol.method

(type_spec name: (type_identifier) @symbol.name type: (struct_type)) @symbol.class

(type_spec name: (type_identifier) @symbol.name type: (interface_type)) @symbol.interface

;; section: calls

;; helper() — bare; same-package or builtin.
(call_expression function: (identifier) @call.callee) @call.range

;; pkg.F(), s.M(), a.b.C() — the operand is read by `_call_qualifier`.
(call_expression
  function: (selector_expression field: (field_identifier) @call.callee)) @call.range

;; section: imports

(import_spec) @import.range
