"""Execution-trace verification of process definitions (X-Ray validation).

The graph is cross-checked three ways already; nothing checked that a
:class:`ProcessDefinition` describes what its flow *does*. This package runs a
flow in the org with a debug trace on, parses the trace, and compares the path
taken and the DML performed against the model.
"""
