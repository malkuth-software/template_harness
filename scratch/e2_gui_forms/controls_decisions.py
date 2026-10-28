"""Controls-area E2 decisions, by the site numbers of controls_ctx.txt (first run, main 4e98cd82)."""

ONCE = "runs once, on the matching item, then returns"
FEW = "a loop over a control's few items; one select per item, and a split would duplicate the body"
AXIS = "maps the logical main and cross axes to physical fields once per item; a split would duplicate the layout body"
SEMANTIC = "publishes one semantic node per item; the flag decides one field of each node"
GUARD = "guards a division per item; a hoisted reciprocal would change the rounding"
WRAP = "steps through a few items with wrap-around; the direction decides each step"
REVISION = "reuses an entry of the previous revision when there is one; the lookup is per item"
PAINT = "paints a few rows or items; the select depends on the item's own state as well"

HOISTED = (4, 11, 15, 39, 40, 46, 54, 57, 59, 68, 70, 74, 85, 87, 88, 90)

DECISIONS = {}
def put(numbers, decision, reason):
    for number in numbers:
        DECISIONS[number] = (decision, reason)

put([1, 13, 16, 17, 18, 19, 42, 60, 89, 101, 104, 105, 106], "keep", ONCE)
put([2, 3, 27, 28], "keep", "aligns a few text lines; one alignment select per line")
put([5, 7, 9], "keep", REVISION)
put([6, 8, 107, 110], "keep", WRAP)
put([10], "keep", "runs once, for the toggled section, then the other sections close")
put([12, 55, 56, 58, 71, 72, 111, 112, 102, 103], "keep", SEMANTIC)
put([14], "keep", "the attempt loop converges the bands; a maximized site stops after its first plan")
put([20], "keep", "two passes over the stacks; the pass picks which stacks it collects")
put([21, 22], "keep", "builds the gallery's fixed demonstration cards once")
put([23, 24, 25, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129,
     131, 132, 133, 134, 135, 136, 138, 144, 145, 151], "keep", AXIS)
put([26], "keep", "a few bindings; a data member prefixes each binding's field")
put([29, 30, 31, 32, 33, 34, 35, 36, 37, 38], "keep", FEW)
put([41], "keep", "a few calendar cells; focus is added only to the selected cell")
put([43, 44], "keep", "formats one pattern token per iteration; the value decides the hour and designator")
put([45, 96, 143, 146], "keep", GUARD)
put([47, 48, 49, 82, 83], "keep", PAINT)
put([50, 51, 52], "keep", "three layout passes converge on the scroll bars; the view picks the per-line rule")
put([53], "keep", "owner-drawn and stock columns share the column walk; a split would duplicate it")
put([61], "keep", "a few pasted pieces; overwrite or insert per piece")
put([62], "keep", "the placement decides whether each character shifts the slots right; a split would duplicate the slot walk")
put([63, 64], "keep", "assembles the text slot by slot; literals and prompts are included on request")
put([65, 66], "keep", "a few months; week numbers add their rows when shown")
put([67], "keep", "single selection stops at the first enabled item; the loop is the search")
put([69], "keep", "the view mode decides each item's glyph and label placement; a split would duplicate the item paint")
put([73], "keep", "runs for the sorted column only")
put([75], "keep", "the sort picks which list each descriptor's rows join")
put([76, 77, 78], "keep", "a property edit over a few selected objects; reset and path decide each object's edit")
put([79, 80], "keep", "a few property rows; the stacked layout adds height and moves the name per row")
put([81], "false_shape", "pop_front() empties undo_ inside the loop, which then trims redo_")
put([84], "keep", "draws each run, then its highlight over the selection, in run order; splitting would change the paint order")
put([86], "keep", "one presentation offset per grapheme; a masked box counts mask characters instead")
put([91, 92, 93, 94, 95], "keep", "a few flames and sparks; reduced motion and orientation shape each one")
put([97], "keep", "a few ticks; one orientation select per tick")
put([98, 99], "keep", "one conversion choice per tile, not per pixel; the tile walk is shared")
put([100], "keep", "commands of other tabs collapse; the selected tab's are placed")
put([108, 109], "keep", "a few palette entries; the key decides the direction of each comparison")
put([130], "keep", "a few children; min-content or max-content per query")
put([137], "keep", "the grow share is per item, and only when the pass assigns")
put([139, 140, 141, 147, 148, 149], "keep", "places or mirrors each child after its slot is solved; one select per child")
put([142], "keep", "a few tracks; desired or minimum extent per track")
put([150], "keep", "a switch on the edge per sibling; a few siblings")
