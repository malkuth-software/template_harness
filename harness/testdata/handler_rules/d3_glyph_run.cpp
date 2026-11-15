// flags: -fexceptions
// D1 from paint. A-gf-core-10-F1: the glyph run rasterizer threw
// std::length_error for ink wider than 16,384 pixels, and SkiaRaster's
// Painter had no handler. The fix reports an oversized run as a result,
// which paint counts and draws nothing for.
#include "vocabulary.hpp"

struct Coverage final {
    std::vector<unsigned char> pixels;
    bool oversized{};
};

constexpr long maximum_extent = 16384;

Coverage rasterize_bad(long width) {
    if (width > maximum_extent) throw std::length_error("Glyph run too large");  // expect: D1
    Coverage coverage{};
    coverage.pixels.assign(static_cast<std::size_t>(width), 0U);
    return coverage;
}

Coverage rasterize_good(long width) noexcept {
    Coverage coverage{};
    if (width > maximum_extent) {
        coverage.oversized = true;
        return coverage;
    }
    coverage.pixels.assign(static_cast<std::size_t>(width), 0U);
    return coverage;
}

class Painter final {
public:
    void draw_text_bad(long width) {
        const Coverage coverage = rasterize_bad(width);
        drawn_ += coverage.pixels.size();
    }

    void draw_text_good(long width) {
        const Coverage coverage = rasterize_good(width);
        if (coverage.oversized) ++oversized_runs_;
        drawn_ += coverage.pixels.size();
    }

    FIXTURE_DISPATCH_ENTRY void paint(long width) {
        draw_text_bad(width);
        draw_text_good(width);
    }

private:
    std::size_t drawn_{};
    std::size_t oversized_runs_{};
};
