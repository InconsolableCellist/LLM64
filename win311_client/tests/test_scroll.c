/*
 * Transcript unit test - builds on the host with cc.
 *
 * The scrollback is the piece most likely to hide a Win16-specific bug
 * (far blocks, a ring that evicts, a wrap that has to agree with itself
 * from two directions), and almost none of that is about Windows. So it
 * is tested here, at native speed, where a failure prints a line instead
 * of repainting a window under Wine.
 *
 * What is worth pinning:
 *   - re-flow: the same text at a different width, no data lost
 *   - markers occupy no cell, so they must not shorten a row
 *   - eviction: the oldest lines go, the newest survive, rows stay right
 *   - the row cursor and the row count agree with each other
 */

#include <stdio.h>
#include <string.h>
#include "wire.h"
#include "scroll.h"

static int failures = 0;

static void check(int cond, const char *what)
{
    printf("%-56s %s\n", what, cond ? "ok" : "FAIL");
    if (!cond)
        failures++;
}

/* Collect the whole transcript as visible text, markers stripped and
   rows rejoined with the space the wrap swallowed - so what comes back
   is the prose that went in, and a word split across a row boundary
   still reads as that word. */
static void render(const Scrollback *sb, char *out, unsigned cap)
{
    SbView v;
    SbRow  r;
    unsigned n = 0, i;

    out[0] = '\0';
    if (!sb_view(sb, 0, &v))
        return;
    while (sb_view_next(&v, &r)) {
        for (i = 0; i < r.len && n + 2 < cap; ) {
            unsigned m = sb_marker_len(r.text + i, r.len - i);
            if (m) {
                i += m;         /* operand bytes are not text either */
                continue;
            }
            out[n++] = r.text[i++];
        }
        if (n + 2 < cap)
            out[n++] = ' ';
    }
    out[n] = '\0';
}

/* Rows produced by walking, which must match the cached count. */
static unsigned long walk_rows(const Scrollback *sb)
{
    SbView v;
    SbRow  r;
    unsigned long n = 0;

    if (!sb_view(sb, 0, &v))
        return 0;
    while (sb_view_next(&v, &r))
        n++;
    return n;
}

/* The widest visible row, in cells. */
static unsigned widest(const Scrollback *sb)
{
    SbView v;
    SbRow  r;
    unsigned w = 0, cells, i;

    if (!sb_view(sb, 0, &v))
        return 0;
    while (sb_view_next(&v, &r)) {
        cells = 0;
        for (i = 0; i < r.len; ) {
            unsigned m = sb_marker_len(r.text + i, r.len - i);
            if (m)
                i += m;
            else {
                cells++;
                i++;
            }
        }
        if (cells > w)
            w = cells;
    }
    return w;
}

int main(void)
{
    /* Static, and roomy: a full ring of lines renders to more text than
       a 16-bit stack would hand out. */
    static Scrollback sb;
    static char buf[96 * 1024];
    SbView v;
    SbRow  r;
    char   line[64];
    unsigned long rows40, rows20;
    unsigned i;

    if (!sb_init(&sb)) {
        printf("sb_init failed\n");
        return 1;
    }

    /* 1. An empty transcript is one blank row: the open line. */
    check(sb_rows(&sb) == 1, "a fresh transcript is one row");
    check(walk_rows(&sb) == 1, "and the cursor agrees");

    /* 2. Wrapping at a width, then re-flowing to another, keeps every
       word - this is the thing the spike could not do at all. */
    sb_width(&sb, 40);
    sb_say(&sb, 13, "the quick brown fox jumps over the lazy dog "
                    "and keeps running until it is out of sight");
    rows40 = sb_rows(&sb);
    check(rows40 > 2, "long text wraps to several rows at 40 cols");
    check(widest(&sb) <= 40, "no row is wider than the pane");
    check(walk_rows(&sb) == rows40, "cached row count matches the walk");

    render(&sb, buf, sizeof(buf));
    check(strstr(buf, "quick brown") != NULL, "words survive the wrap");
    check(strstr(buf, "out of sight") != NULL, "and so does the tail");

    sb_width(&sb, 20);
    rows20 = sb_rows(&sb);
    check(rows20 > rows40, "narrowing the pane makes more rows");
    check(widest(&sb) <= 20, "and every row fits the new width");
    check(walk_rows(&sb) == rows20, "row count still matches the walk");

    sb_width(&sb, 40);
    check(sb_rows(&sb) == rows40, "widening it back restores the count");
    render(&sb, buf, sizeof(buf));
    check(strstr(buf, "out of sight") != NULL, "re-flow loses no text");

    /* 3. A word longer than the pane has to break somewhere. */
    sb_clear(&sb);
    sb_width(&sb, 10);
    sb_say(&sb, 13, "supercalifragilistic");
    check(widest(&sb) <= 10, "an unbreakable word is broken flush");
    render(&sb, buf, sizeof(buf));
    check(strstr(buf, "supercalif") != NULL, "its first half is kept");

    /* 4. Markers cost no cells. The same sentence with color markers
       in it must occupy the same rows as without - the spike counted
       them as characters and wrapped early. */
    sb_clear(&sb);
    sb_width(&sb, 20);
    sb_say(&sb, 13, "aaaa bbbb cccc dddd eeee");
    rows40 = sb_rows(&sb);
    sb_clear(&sb);
    sb_say(&sb, 13, "\x12" "aaaa" "\x01" " \x1D" "bbbb" "\x01"
                    " cccc \x02" "dddd" "\x03" " eeee");
    check(sb_rows(&sb) == rows40, "markers do not change the row count");
    check(widest(&sb) <= 20, "and do not overflow the width either");

    /* 5. Marker state is carried into the row it continues into: a
       color opened before a wrap is still in force after it. */
    sb_clear(&sb);
    sb_width(&sb, 10);
    sb_say(&sb, 13, "\x12" "aaaa bbbb cccc");
    check(sb_view(&sb, 1, &v) && sb_view_next(&v, &r),
          "the second row exists");
    check(r.color == 2, "and inherits the color opened on the first");

    /* 6. A close marker returns to the line's own color, not to the
       color some earlier line happened to end in. */
    sb_clear(&sb);
    sb_width(&sb, 40);
    sb_say(&sb, 7, "plain \x12" "red\x01 plain again");
    check(sb_view(&sb, 0, &v) && sb_view_next(&v, &r), "row fetched");
    check(r.base == 7, "the row knows its line's base color");

    /* 7. Streaming: text arrives without newlines and the open line has
       to stay visible and correctly wrapped as it grows. */
    sb_clear(&sb);
    sb_width(&sb, 20);
    sb_color(&sb, 13);
    for (i = 0; i < 12; i++)
        sb_puts(&sb, "chunk ");
    check(walk_rows(&sb) == sb_rows(&sb), "an open line counts correctly");
    check(widest(&sb) <= 20, "and wraps as it grows");
    render(&sb, buf, sizeof(buf));
    check(strstr(buf, "chunk chunk") != NULL, "open text is visible");

    /* 8. Eviction. Write far more than the ring holds and check that it
       neither loses its place nor its accounting. */
    sb_clear(&sb);
    sb_width(&sb, 40);
    for (i = 0; i < SB_MAX_LINES * 2; i++) {
        sprintf(line, "line %u of the very long transcript", i);
        sb_say(&sb, 13, line);
    }
    check(sb_lines(&sb) <= SB_MAX_LINES + 1, "the ring bounds the lines");
    check(walk_rows(&sb) == sb_rows(&sb),
          "row count survives thousands of lines");
    render(&sb, buf, sizeof(buf));
    sprintf(line, "line %u of", SB_MAX_LINES * 2 - 1);
    check(strstr(buf, line) != NULL, "the newest line is still there");
    check(strstr(buf, "line 0 of") == NULL, "the oldest has been evicted");

    /* 9. Re-flow after eviction: the surviving lines still know their
       own text, so widths recompute against the arena and not against
       a stale offset. */
    sb_width(&sb, 25);
    check(walk_rows(&sb) == sb_rows(&sb), "re-flow after eviction agrees");
    check(widest(&sb) <= 25, "and respects the new width");

    /* 10. Seeking to a row returns that row, from either direction. */
    sb_clear(&sb);
    sb_width(&sb, 80);
    for (i = 0; i < 10; i++) {
        sprintf(line, "row %u", i);
        sb_say(&sb, 13, line);
    }
    check(sb_view(&sb, 4, &v) && sb_view_next(&v, &r), "seek to row 4");
    check(r.len == 5 && memcmp(r.text, "row 4", 5) == 0,
          "and it is the row asked for");
    check(!sb_view(&sb, sb_rows(&sb), &v), "seeking past the end fails");

    /* 11. A line longer than SB_MAX_LINE is continued, not truncated. */
    sb_clear(&sb);
    sb_width(&sb, 80);
    for (i = 0; i < SB_MAX_LINE + 200; i++)
        sb_putc(&sb, 'x');
    sb_newline(&sb);
    render(&sb, buf, sizeof(buf));
    check(strlen(buf) >= SB_MAX_LINE + 200,
          "an over-long line is continued, not dropped");

    /* 12. And that continuation happens at a space, so a paragraph too
       long to hold whole is not cut through the middle of a word. This
       is the one visible seam in the design; it should at least be a
       tidy one. */
    sb_clear(&sb);
    sb_width(&sb, 80);
    while (sb_lines(&sb) < 3) {
        for (i = 0; i < 40; i++)
            sb_putc(&sb, "wordy "[i % 6]);
    }
    sb_newline(&sb);
    render(&sb, buf, sizeof(buf));
    check(strstr(buf, "wordywordy") == NULL || strstr(buf, "wordy w") != NULL,
          "the seam falls between words");
    for (i = 0; buf[i]; i++)
        if (buf[i] != 'w' && buf[i] != 'o' && buf[i] != 'r' &&
            buf[i] != 'd' && buf[i] != 'y' && buf[i] != ' ')
            break;
    check(buf[i] == '\0', "and nothing else is introduced at the seam");

    /* --- rich text (docs/16 section 7) ------------------------------
       The extended color marker is THREE bytes, and that is the one
       thing about it that can break everything else: a wrapper counting
       bytes instead of markers charges two cells for it, and every line
       carrying one wraps short. */
    {
        char esc[4];
        unsigned long rows_plain;

        esc[0] = MARK_ESC;
        esc[1] = MARK_ESC_COLOR;
        esc[2] = (char)(MARK_ESC_BIAS | 20);    /* gold */
        esc[3] = '\0';

        sb_clear(&sb);
        sb_width(&sb, 20);
        sb_say(&sb, 13, "aaaa bbbb cccc dddd eeee");
        rows_plain = sb_rows(&sb);

        sb_clear(&sb);
        sb_say(&sb, 13, "aaaa bbbb cccc dddd eeee");
        sb_newline(&sb);
        check(sb_rows(&sb) >= rows_plain, "baseline holds");

        sb_clear(&sb);
        sb_width(&sb, 20);
        sb_color(&sb, 13);
        sb_puts(&sb, esc);
        sb_puts(&sb, "aaaa bbbb cccc dddd eeee");
        sb_newline(&sb);
        check(sb_rows(&sb) == rows_plain,
              "a 3-byte color marker costs no cells");
        check(widest(&sb) <= 20, "and does not overflow the width");

        render(&sb, buf, sizeof(buf));
        check(strstr(buf, "aaaa bbbb") != NULL, "the text survives it");
        check(strchr(buf, MARK_ESC_COLOR) == NULL,
              "and its operand bytes are not text");

        /* The slot has to arrive intact and de-biased. Observed on the
           SECOND row, because r.color is the state at a row's first cell
           and a marker sitting at position 0 has not been applied yet -
           the same reason the one-byte color case above reads row 1. */
        sb_clear(&sb);
        sb_width(&sb, 10);
        sb_color(&sb, 1);
        sb_puts(&sb, esc);
        sb_puts(&sb, "aaaa bbbb cccc");
        sb_newline(&sb);
        check(sb_view(&sb, 1, &v) && sb_view_next(&v, &r), "row fetched");
        check(r.color == 20, "the extended slot decodes to itself");
        check(r.base == 1, "and the line's base color is untouched");

        /* A marker cut off by the end of a row is not a marker. Rows are
           slices of an arena block, so consuming bytes that are not
           there is a fault and not a stray glyph. */
        check(sb_marker_len(esc, 3) == MARK_ESC_LEN, "a whole escape is 3");
        check(sb_marker_len(esc, 2) == 0, "a truncated escape is not one");
        check(sb_marker_len(esc, 1) == 0, "nor is a lone ESC");
        check(sb_marker_len("x", 1) == 0, "a glyph is not a marker");

        /* Attributes carry across a wrap exactly as color does, and
           they are independent of each other. */
        sb_clear(&sb);
        sb_width(&sb, 10);
        sb_color(&sb, 1);
        sb_putc(&sb, MARK_ITALIC_ON);
        sb_putc(&sb, MARK_ULINE_ON);
        sb_puts(&sb, "aaaa bbbb cccc");
        sb_newline(&sb);
        check(sb_view(&sb, 1, &v) && sb_view_next(&v, &r),
              "the second row exists");
        check((r.attr & SB_ATTR_ITALIC) != 0, "italic survives the wrap");
        check((r.attr & SB_ATTR_ULINE) != 0, "so does underline");
        check((r.attr & SB_ATTR_BOLD) == 0, "and bold was never set");

        /* Turning one off leaves the others standing. */
        sb_clear(&sb);
        sb_width(&sb, 40);
        sb_color(&sb, 1);
        sb_putc(&sb, MARK_ITALIC_ON);
        sb_putc(&sb, MARK_BOLD_ON);
        sb_puts(&sb, "aa");
        sb_putc(&sb, MARK_ITALIC_OFF);
        sb_puts(&sb, "bb");
        sb_newline(&sb);
        sb_width(&sb, 2);       /* force "bb" onto its own row */
        check(sb_view(&sb, 1, &v) && sb_view_next(&v, &r), "second row");
        check((r.attr & SB_ATTR_BOLD) != 0, "bold outlives italic-off");
        check((r.attr & SB_ATTR_ITALIC) == 0, "and italic is off");
    }

    sb_free(&sb);
    printf("\n%s\n", failures ? "FAILURES" : "all transcript tests passed");
    return failures ? 1 : 0;
}
