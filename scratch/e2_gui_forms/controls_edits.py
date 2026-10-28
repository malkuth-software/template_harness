import pathlib
import sys

root = pathlib.Path("D:/gui_forms_w3/gui_forms/src/controls")

edits = {
    "panel/list_box/list_box.cpp": [(
        """    const std::size_t end = std::min(items_.size(), top_index_ + visible);
    for (std::size_t index = top_index_; index < end; ++index) {""",
        """    const std::size_t end = std::min(items_.size(), top_index_ + visible);
    const Color selected_fill = focused_ ? style().accent : style().accent_light;
    for (std::size_t index = top_index_; index < end; ++index) {"""), (
        """            painter.fill_rect(row, focused_ ? style().accent
                                            : style().accent_light);""",
        """            painter.fill_rect(row, selected_fill);""")],
    "panel/object_view/object_view.cpp": [(
        """    std::optional<std::size_t> focused_truncated;
    for (std::size_t index = first; index < end; ++index) {""",
        """    std::optional<std::size_t> focused_truncated;
    const Color selected_fill = focused_ ? style().accent_light : with_alpha(style().accent_light, 190);
    for (std::size_t index = first; index < end; ++index) {"""), (
        """            painter.fill_rect({cell.x + 1.0, cell.y + 1.0,
                               cell.width - 2.0, cell.height - 2.0},
                              focused_ ? style().accent_light
                                       : with_alpha(style().accent_light, 190));""",
        """            painter.fill_rect({cell.x + 1.0, cell.y + 1.0,
                               cell.width - 2.0, cell.height - 2.0},
                              selected_fill);"""), (
        """            double baseline = cell.y + std::max(57.0, 47.0 + font.size);
            for (const std::string& line : label.lines) {""",
        """            double baseline = cell.y + std::max(57.0, 47.0 + font.size);
            const Color label_colour = item.enabled ? style().text : style().disabled_text;
            for (const std::string& line : label.lines) {"""), (
        """                painter.draw_text_utf8(
                    {text_x, baseline}, line, font,
                    item.enabled ? style().text : style().disabled_text);""",
        """                painter.draw_text_utf8({text_x, baseline}, line, font, label_colour);"""), (
        """    painter.clip_rect({4.0, 2.0 + header, viewport_right - 4.0, std::max(0.0, bounds.height - header - 4.0)});
    for (std::size_t item_index_value = first; item_index_value < end; ++item_index_value) {""",
        """    painter.clip_rect({4.0, 2.0 + header, viewport_right - 4.0, std::max(0.0, bounds.height - header - 4.0)});
    const Color selected_row_fill = focused_ ? style().accent_light : with_alpha(style().accent_light, 190);
    for (std::size_t item_index_value = first; item_index_value < end; ++item_index_value) {"""), (
        """            painter.fill_rect(row, focused_ ? style().accent_light : with_alpha(style().accent_light, 190));""",
        """            painter.fill_rect(row, selected_row_fill);""")],
    "panel/tree_view/tree_view.cpp": [(
        """    const std::size_t end = std::min(visible_.size(), top_row_ + visible_row_count() + 1U);
    for (std::size_t row = top_row_; row < end; ++row) {""",
        """    const std::size_t end = std::min(visible_.size(), top_row_ + visible_row_count() + 1U);
    const Color selected_fill = focused_ ? style().accent : style().accent_light;
    for (std::size_t row = top_row_; row < end; ++row) {"""), (
        """            painter.fill_rect({2.0, y, std::max(0.0, bounds.width - 4.0), row_height},
                              focused_ ? style().accent : style().accent_light);""",
        """            painter.fill_rect({2.0, y, std::max(0.0, bounds.width - 4.0), row_height}, selected_fill);""")],
    "panel/text_box/text_box.cpp": [(
        """    const Color foreground = themed ? editor.text : enabled() ? style().text : style().disabled_text;
    painter.save();
    painter.clip_rect({text_left_, 4.0, width, height});""",
        """    const Color foreground = themed ? editor.text : enabled() ? style().text : style().disabled_text;
    const Color selected_text = themed ? highlight.text : style().highlight;
    painter.save();
    painter.clip_rect({text_left_, 4.0, width, height});"""), (
        """                painter.draw_text_utf8(baseline, value, font, themed ? highlight.text : style().highlight);""",
        """                painter.draw_text_utf8(baseline, value, font, selected_text);""")],
}

failed = 0
for name, pairs in edits.items():
    path = root / name
    data = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in data
    text = data.replace("\r\n", "\n")
    for old, new in pairs:
        count = text.count(old)
        if count != 1:
            print("MISS", name, count, old[:70])
            failed += 1
            continue
        text = text.replace(old, new)
    if crlf:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
    print("ok", name)
sys.exit(1 if failed else 0)
