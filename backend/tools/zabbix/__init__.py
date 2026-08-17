"""Zabbix query branches.

`zabbix_tool.py` holds the single LangChain tool and its description; this
package holds the implementations it dispatches to, one module per kind of
question. The split is internal — the tool's name, arguments and description
are unchanged, so nothing the model sees depends on it.
"""
