"""uslm -- state law (and anything else that is not already USLM) into USLM 1.0 XML.

    families/   one parser per publishing format, each config-driven (data/state_formulas.toml)
    tree.py     the common tree and the numbering ladder every family shares
    writer.py   tree -> USLM
    verify.py   conservation, schema, numbering, identifiers
    build.py    formula + file -> USLM file + verify report
"""
