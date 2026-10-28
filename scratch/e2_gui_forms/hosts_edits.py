import pathlib
import sys

root = pathlib.Path("D:/gui_forms_w3/gui_forms/src/host/windows/application")

edits = {
    "windows_host.cpp": [(
        # stroke_rounded_rect: an empty inner rectangle fills each row's outer span.
        """        const double inner_radius = std::max(0.0, radius - line);
        for (int y = area.top; y < area.bottom; ++y) {
            std::uint32_t* row = row_at(y);
            const PixelSpan outer = rounded_row_span(rect, radius, y, clipped_row_span(area, y));
            const PixelSpan inset = inner.empty() ? PixelSpan{} : rounded_row_span(inner, inner_radius, y, outer);
            if (inset.right > inset.left) {""",
        """        const double inner_radius = std::max(0.0, radius - line);
        // A stroke as wide as the rectangle leaves no inside: every row is filled.
        if (inner.empty()) {
            for (int y = area.top; y < area.bottom; ++y) {
                std::uint32_t* row = row_at(y);
                fill_span(row, rounded_row_span(rect, radius, y, clipped_row_span(area, y)), color, color.alpha);
            }
            return;
        }
        for (int y = area.top; y < area.bottom; ++y) {
            std::uint32_t* row = row_at(y);
            const PixelSpan outer = rounded_row_span(rect, radius, y, clipped_row_span(area, y));
            const PixelSpan inset = rounded_row_span(inner, inner_radius, y, outer);
            if (inset.right > inset.left) {"""), (
        # fill_linear_gradient_spread: vertical, horizontal and general gradients each have their loop.
        """        for (int y = area.top; y < area.bottom; ++y) {
            std::uint32_t* row = row_at(y);
            const PixelSpan span = clipped_row_span(area, y);
            if (dx == 0.0) {
                const double amount = spread_gradient_coordinate(
                    (((y + 0.5) / scale_ - start.y) * dy) / length_squared, spread);
                const Color color = gradient_color(stops, amount);
                fill_span(row, span, color, color.alpha);
                continue;
            }
            for (int x = span.left; x < span.right; ++x) {
                if (!columns.empty()) {
                    const Color color = columns[static_cast<std::size_t>(x - area.left)];
                    blend_pixel(row[x], color, color.alpha);
                    continue;
                }
                const Point sample{(x + 0.5) / scale_, (y + 0.5) / scale_};""",
        """        // A vertical gradient has one colour per row.
        if (dx == 0.0) {
            for (int y = area.top; y < area.bottom; ++y) {
                std::uint32_t* row = row_at(y);
                const PixelSpan span = clipped_row_span(area, y);
                const double amount = spread_gradient_coordinate(
                    (((y + 0.5) / scale_ - start.y) * dy) / length_squared, spread);
                const Color color = gradient_color(stops, amount);
                fill_span(row, span, color, color.alpha);
            }
            return;
        }
        // A horizontal one has one colour per column, computed above.
        if (!columns.empty()) {
            for (int y = area.top; y < area.bottom; ++y) {
                std::uint32_t* row = row_at(y);
                const PixelSpan span = clipped_row_span(area, y);
                for (int x = span.left; x < span.right; ++x) {
                    const Color color = columns[static_cast<std::size_t>(x - area.left)];
                    blend_pixel(row[x], color, color.alpha);
                }
            }
            return;
        }
        for (int y = area.top; y < area.bottom; ++y) {
            std::uint32_t* row = row_at(y);
            const PixelSpan span = clipped_row_span(area, y);
            for (int x = span.left; x < span.right; ++x) {
                const Point sample{(x + 0.5) / scale_, (y + 0.5) / scale_};"""), (
        # gradient_raster: radial and linear rasters each have their loop.
        """        const double squared = dx * dx + dy * dy;
        for (int y = area.top; y < area.bottom; ++y) {
            for (int x = area.left; x < area.right; ++x) {
                const Point sample{(x + 0.5) / scale_, (y + 0.5) / scale_};
                double amount{};
                if (radial) {
                    const double rx = (sample.x - first.x) / second.x;
                    const double ry = (sample.y - first.y) / second.y;
                    amount = std::sqrt(rx * rx + ry * ry);
                } else {
                    amount = spread_gradient_coordinate(
                        ((sample.x - first.x) * dx + (sample.y - first.y) * dy) / squared, spread);
                }
                const Color color = gradient_color(stops, amount);
                raster.pixels.push_back(color.blue | (static_cast<std::uint32_t>(color.green) << 8U) |
                    (static_cast<std::uint32_t>(color.red) << 16U) | (static_cast<std::uint32_t>(color.alpha) << 24U));
            }
        }""",
        """        const double squared = dx * dx + dy * dy;
        // A radial raster's amount is the distance from the centre in radii;
        // a linear one's is the position along the gradient's line.
        if (radial) {
            for (int y = area.top; y < area.bottom; ++y) {
                for (int x = area.left; x < area.right; ++x) {
                    const Point sample{(x + 0.5) / scale_, (y + 0.5) / scale_};
                    const double rx = (sample.x - first.x) / second.x;
                    const double ry = (sample.y - first.y) / second.y;
                    const double amount = std::sqrt(rx * rx + ry * ry);
                    raster.pixels.push_back(packed_argb(gradient_color(stops, amount)));
                }
            }
        } else {
            for (int y = area.top; y < area.bottom; ++y) {
                for (int x = area.left; x < area.right; ++x) {
                    const Point sample{(x + 0.5) / scale_, (y + 0.5) / scale_};
                    const double amount = spread_gradient_coordinate(
                        ((sample.x - first.x) * dx + (sample.y - first.y) * dy) / squared, spread);
                    raster.pixels.push_back(packed_argb(gradient_color(stops, amount)));
                }
            }
        }"""), (
        # paint_gradient_raster: an opaque raster copies its rows.
        """        const int stride = area.right - area.left;
        for (int y = area.top; y < area.bottom; ++y) {
            const PixelSpan span = clipped_row_span(area, y);
            if (span.right <= span.left) continue;
            const std::uint32_t* source = raster.pixels.data() +
                static_cast<std::size_t>(y - area.top) * stride;
            std::uint32_t* row = row_at(y);
            if (raster.opaque) {
                std::copy(source + span.left - area.left, source + span.right - area.left, row + span.left);
                continue;
            }
            for (int x = span.left; x < span.right; ++x) {""",
        """        const int stride = area.right - area.left;
        // An opaque raster replaces the pixels under it: each row is a copy.
        if (raster.opaque) {
            for (int y = area.top; y < area.bottom; ++y) {
                const PixelSpan span = clipped_row_span(area, y);
                if (span.right <= span.left) continue;
                const std::uint32_t* source = raster.pixels.data() +
                    static_cast<std::size_t>(y - area.top) * stride;
                std::copy(source + span.left - area.left, source + span.right - area.left, row_at(y) + span.left);
            }
            return;
        }
        for (int y = area.top; y < area.bottom; ++y) {
            const PixelSpan span = clipped_row_span(area, y);
            if (span.right <= span.left) continue;
            const std::uint32_t* source = raster.pixels.data() +
                static_cast<std::size_t>(y - area.top) * stride;
            std::uint32_t* row = row_at(y);
            for (int x = span.left; x < span.right; ++x) {"""), (
        """    [[nodiscard]] static std::uint32_t sample_image_pixel(""",
        """    // A colour as a gradient raster stores it: 0xAARRGGBB.
    [[nodiscard]] static std::uint32_t packed_argb(Color color) noexcept {
        const std::uint32_t packed = color.blue | (static_cast<std::uint32_t>(color.green) << 8U) |
            (static_cast<std::uint32_t>(color.red) << 16U) | (static_cast<std::uint32_t>(color.alpha) << 24U);
        return packed;
    }
    [[nodiscard]] static std::uint32_t sample_image_pixel(""")],
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
