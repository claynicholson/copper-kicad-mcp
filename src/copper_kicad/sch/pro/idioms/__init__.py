"""Idioms: known sub-circuits drawn from rigid, netlist-checked templates (see docs/internal/SCHEMATIC_PRO_V2_DESIGN.md).

    match.match_block(D, info)      read-only: which parts of a block form which idiom
    passive.*_template(...)         how each passive idiom is drawn (pure geometry)
    model.instantiate(D, m, t)      pose the parts, prove every wire against the netlist, or raise IdiomError

`build.Blk` asks for templates while it places an anchor; a match whose template cannot be proven or does not fit
is dropped and its parts are drawn the legacy way (stub + label)."""
