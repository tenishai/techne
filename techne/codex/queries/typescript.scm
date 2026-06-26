;; TypeScript tree-sitter query patterns for Codex symbol + call extraction (techne#89).
;;
;; Adapted from the basemind project (github.com/Goldziher/basemind, MIT licence).
;; See NOTICE for attribution. Capture names remapped to match techne's
;; extraction layer; implementation/interface-extends patterns omitted (not used in v1).
;;
;; section: symbols

(function_declaration name: (identifier) @symbol.name) @symbol.function

(class_declaration name: (type_identifier) @symbol.name) @symbol.class

(interface_declaration name: (type_identifier) @symbol.name) @symbol.interface

(method_definition name: (property_identifier) @symbol.name) @symbol.method

;; Arrow functions assigned to const: const Foo = () => ...
(lexical_declaration
  (variable_declarator
    name: (identifier) @symbol.name
    value: (arrow_function))) @symbol.function

;; Function expressions assigned to const: const Bar = function() { ... }
(lexical_declaration
  (variable_declarator
    name: (identifier) @symbol.name
    value: (function_expression))) @symbol.function

;; section: calls

(call_expression function: (identifier) @call.callee) @call.range
(call_expression
  function: (member_expression property: (property_identifier) @call.callee)) @call.range

;; section: imports

(import_statement) @import.range
