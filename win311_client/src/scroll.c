/*
 * LLM64 for Windows - the transcript
 *
 * See include/scroll.h for why this exists. The short version: unwrapped
 * lines in far blocks, wrapped at paint time by one iterator.
 */

#include <stdlib.h>
#include <string.h>
#include "wire.h"
#include "scroll.h"

/* ---------------------------------------------------------------- */
/* Wrapping                                                          */
/* ---------------------------------------------------------------- */

int sb_is_marker(unsigned char c)
{
    return c == MARK_CLOSE
        || c == MARK_BOLD_ON   || c == MARK_BOLD_OFF
        || c == MARK_ITALIC_ON || c == MARK_ITALIC_OFF
        || c == MARK_ULINE_ON  || c == MARK_ULINE_OFF
        || c == MARK_HEAD_ON   || c == MARK_HEAD_OFF
        || c == MARK_ESC
        || (c >= MARK_COLOR_BASE + 1 && c <= MARK_COLOR_BASE + 15);
}

unsigned sb_marker_len(const char *p, unsigned remaining)
{
    unsigned char c;

    if (!remaining)
        return 0;
    c = (unsigned char)p[0];
    if (c != MARK_ESC)
        return sb_is_marker(c) ? 1 : 0;

    /* An escape whose operands did not fit is not a marker. Treating it
       as one would consume bytes that belong to the next row, and the
       row is a slice of an arena block - there is nothing safe past it
       to consume. */
    if (remaining < MARK_ESC_LEN || (unsigned char)p[1] != MARK_ESC_COLOR)
        return 0;
    return MARK_ESC_LEN;
}

void sb_mark_apply(const char *p, unsigned len, unsigned char base,
                   unsigned char *color, unsigned char *attr)
{
    unsigned char c = (unsigned char)p[0];

    if (c == MARK_ESC) {
        if (len == MARK_ESC_LEN)
            *color = (unsigned char)((unsigned char)p[2] & ~MARK_ESC_BIAS);
        return;
    }
    if (c == MARK_CLOSE)              *color = base;
    else if (c == MARK_BOLD_ON)       *attr |= SB_ATTR_BOLD;
    else if (c == MARK_BOLD_OFF)      *attr &= (unsigned char)~SB_ATTR_BOLD;
    else if (c == MARK_ITALIC_ON)     *attr |= SB_ATTR_ITALIC;
    else if (c == MARK_ITALIC_OFF)    *attr &= (unsigned char)~SB_ATTR_ITALIC;
    else if (c == MARK_ULINE_ON)      *attr |= SB_ATTR_ULINE;
    else if (c == MARK_ULINE_OFF)     *attr &= (unsigned char)~SB_ATTR_ULINE;
    else if (c == MARK_HEAD_ON)       *attr |= SB_ATTR_HEAD;
    else if (c == MARK_HEAD_OFF)      *attr &= (unsigned char)~SB_ATTR_HEAD;
    else                              *color = (unsigned char)(c & 0x0F);
}

void sb_wrap_begin(SbWrap *w, const char *text, unsigned len,
                   unsigned cols, unsigned char base)
{
    w->text  = text;
    w->len   = len;
    w->cols  = cols < 1 ? 1 : cols;
    w->pos   = 0;
    w->color = base;
    w->base  = base;
    w->attr  = 0;
    w->done  = 0;
}

int sb_wrap_next(SbWrap *w, SbRow *out)
{
    unsigned start, i, cells, brk, next, mlen;
    int      soft;
    unsigned char color, attr;

    if (w->done)
        return 0;

    start = w->pos;
    out->text  = w->text + start;
    out->color = w->color;
    out->base  = w->base;
    out->attr  = w->attr;

    /* Walk forward a row's worth of *cells*. Markers are free: they are
       state changes, not glyphs, which is the whole reason the spike's
       len-counting wrap broke a coloured line early. */
    color = w->color;
    attr  = w->attr;
    brk   = 0;
    soft  = 0;
    cells = 0;
    for (i = start; i < w->len && cells < w->cols; ) {
        unsigned char c = (unsigned char)w->text[i];
        mlen = sb_marker_len(w->text + i, w->len - i);
        if (mlen) {
            sb_mark_apply(w->text + i, mlen, w->base, &color, &attr);
            i += mlen;
            continue;
        }
        if (c == ' ') {
            brk  = i;      /* the last place we could break cleanly */
            soft = 1;
        }
        cells++;
        i++;
    }

    /* Markers sitting right on the break belong to the row that ends
       there, not to a row of their own - otherwise a colour span that
       closes exactly at the margin produces a blank line. */
    while (i < w->len
           && (mlen = sb_marker_len(w->text + i, w->len - i)) != 0) {
        sb_mark_apply(w->text + i, mlen, w->base, &color, &attr);
        i += mlen;
    }

    if (i >= w->len) {
        /* The rest of the line fits. */
        out->len = w->len - start;
        w->done  = 1;
        return 1;
    }

    /* More text remains, so this row has to end somewhere. Prefer the
       last space; if the row is one long token, break it flush. */
    if (soft && brk > start) {
        next = brk + 1;
        /* A run of spaces at a wrap point is between words, so it is
           swallowed rather than indenting the continuation. */
        while (next < w->len && w->text[next] == ' ')
            next++;
    } else {
        brk  = i;
        next = i;
    }
    out->len = brk - start;

    /* The state at the break is not the state we scanned to: markers
       between the break and the scan point belong to the next row. Walk
       the row we are actually emitting to get it right. */
    color = w->color;
    attr  = w->attr;
    for (i = start; i < next; ) {
        mlen = sb_marker_len(w->text + i, next - i);
        if (mlen) {
            sb_mark_apply(w->text + i, mlen, w->base, &color, &attr);
            i += mlen;
        } else {
            i++;
        }
    }
    w->pos   = next;
    w->color = color;
    w->attr  = attr;
    if (next >= w->len)
        w->done = 1;
    return 1;
}

static unsigned wrap_rows(const char *text, unsigned len, unsigned cols,
                          unsigned char base)
{
    SbWrap w;
    SbRow  r;
    unsigned n = 0;

    sb_wrap_begin(&w, text, len, cols, base);
    while (sb_wrap_next(&w, &r))
        n++;
    return n ? n : 1;
}

/* ---------------------------------------------------------------- */
/* The open line                                                     */
/* ---------------------------------------------------------------- */

/* Re-wrap only from the start of the open line's final row. Appending a
   character can complete at most a row at a time, so this is a row's
   worth of work per call rather than a whole line's. */
static void open_reflow(Scrollback *sb)
{
    SbWrap w;
    SbRow  r;

    for (;;) {
        sb_wrap_begin(&w, sb->open + sb->open_tail,
                      sb->open_len - sb->open_tail, sb->cols,
                      sb->open_color);
        w.color = sb->open_tail_color;
        if (!sb_wrap_next(&w, &r) || w.done)
            return;
        sb->open_rows++;
        sb->open_tail += w.pos;
        sb->open_tail_color = w.color;
    }
}

static void open_reset(Scrollback *sb)
{
    sb->open_len        = 0;
    sb->open_tail       = 0;
    sb->open_rows       = 0;
    sb->open_color      = sb->color;
    sb->open_tail_color = sb->color;
}

/* ---------------------------------------------------------------- */
/* The arena                                                         */
/* ---------------------------------------------------------------- */

static void drop_oldest(Scrollback *sb)
{
    if (sb->count == 0)
        return;
    sb->total_rows -= sb->lines[sb->head].rows;
    sb->head = (sb->head + 1) % SB_MAX_LINES;
    sb->count--;
}

/* Make room for len bytes and return where they go, or 0 if the line is
   impossibly long. Blocks are filled in order and recycled in order, so
   the lines living in the block about to be reused are exactly the
   oldest ones - which is why eviction is a walk from the head and not a
   search. */
static int arena_reserve(Scrollback *sb, unsigned len,
                         unsigned *block, unsigned *off)
{
    if (len > SB_BLOCK_SIZE)
        return 0;

    if (sb->cur_off + len > SB_BLOCK_SIZE) {
        unsigned next = sb->cur_block + 1;

        if (next >= SB_MAX_BLOCKS)
            next = 0;
        if (next >= sb->nblocks) {
            /* Grow while we still can; fall back to recycling if the
               global heap says no. */
            char *b = (char *)malloc(SB_BLOCK_SIZE);
            if (b) {
                sb->blocks[sb->nblocks] = b;
                next = sb->nblocks;
                sb->nblocks++;
            } else {
                next = 0;
            }
        }
        if (next < sb->nblocks && sb->blocks[next]) {
            while (sb->count > 0 && sb->lines[sb->head].block == next)
                drop_oldest(sb);
            sb->cur_block = next;
            sb->cur_off   = 0;
        } else {
            return 0;
        }
    }

    *block = sb->cur_block;
    *off   = sb->cur_off;
    sb->cur_off += len;
    return 1;
}

/* Move the open line into the arena as a committed line. */
static void commit(Scrollback *sb)
{
    unsigned block = 0, off = 0, slot;
    unsigned len = sb->open_len;
    SbLine  *ln;

    if (len > 0 && !arena_reserve(sb, len, &block, &off))
        len = 0;
    if (len == 0) {
        /* An empty line still takes a slot: blank lines are how the
           transcript spaces one turn from the next. */
        block = sb->cur_block;
        off   = sb->cur_off;
    } else {
        memcpy(sb->blocks[block] + off, sb->open, len);
    }

    if (sb->count >= SB_MAX_LINES)
        drop_oldest(sb);
    slot = (sb->head + sb->count) % SB_MAX_LINES;
    ln = &sb->lines[slot];
    ln->block = block;
    ln->off   = off;
    ln->len   = len;
    ln->color = sb->open_color;
    ln->who   = sb->origin;
    ln->rows  = len ? wrap_rows(sb->blocks[block] + off, len, sb->cols,
                                ln->color)
                    : 1;
    sb->count++;
    sb->total_rows += ln->rows;

    open_reset(sb);
}

/* ---------------------------------------------------------------- */
/* Public                                                            */
/* ---------------------------------------------------------------- */

int sb_init(Scrollback *sb)
{
    memset(sb, 0, sizeof(*sb));
    sb->cols  = 80;
    sb->color = 13;

    sb->lines = (SbLine *)malloc(sizeof(SbLine) * SB_MAX_LINES);
    if (!sb->lines)
        return 0;
    sb->blocks[0] = (char *)malloc(SB_BLOCK_SIZE);
    if (!sb->blocks[0]) {
        free(sb->lines);
        sb->lines = 0;
        return 0;
    }
    sb->nblocks = 1;
    open_reset(sb);
    return 1;
}

void sb_free(Scrollback *sb)
{
    unsigned i;

    for (i = 0; i < sb->nblocks; i++) {
        if (sb->blocks[i]) {
            free(sb->blocks[i]);
            sb->blocks[i] = 0;
        }
    }
    sb->nblocks = 0;
    if (sb->lines) {
        free(sb->lines);
        sb->lines = 0;
    }
}

void sb_clear(Scrollback *sb)
{
    sb->head       = 0;
    sb->count      = 0;
    sb->total_rows = 0;
    sb->cur_block  = 0;
    sb->cur_off    = 0;
    open_reset(sb);
}

void sb_color(Scrollback *sb, unsigned char color)
{
    sb->color = color;
    if (sb->open_len == 0)
        open_reset(sb);
}

void sb_origin(Scrollback *sb, unsigned char who)
{
    sb->origin = who;
}

void sb_putc(Scrollback *sb, char c)
{
    if (c == '\n') {
        commit(sb);
        return;
    }
    if (c == '\r')
        return;
    if (sb->open_len >= SB_MAX_LINE) {
        /* A line this long is a proxy that never sent a newline. Break
           it rather than dropping the rest of the reply on the floor,
           and break it at a space: the seam costs a short row either
           way, but it should not also cut a word in half. */
        unsigned i, brk = 0, tail;
        char carry[SB_CARRY];

        for (i = sb->open_len; i > 0 && i + SB_CARRY > sb->open_len; i--) {
            if (sb->open[i - 1] == ' ') { brk = i - 1; break; }
        }
        if (brk > 0) {
            tail = sb->open_len - brk - 1;
            memcpy(carry, sb->open + brk + 1, tail);
            sb->open_len = brk;
            commit(sb);
            memcpy(sb->open, carry, tail);
            sb->open_len = tail;
            open_reflow(sb);
        } else {
            commit(sb);
        }
    }
    sb->open[sb->open_len++] = c;
    open_reflow(sb);
}

void sb_puts(Scrollback *sb, const char *s)
{
    while (*s)
        sb_putc(sb, *s++);
}

void sb_newline(Scrollback *sb)
{
    commit(sb);
}

void sb_say(Scrollback *sb, unsigned char color, const char *s)
{
    if (sb->open_len > 0)
        commit(sb);
    sb_color(sb, color);
    sb_puts(sb, s);
    commit(sb);
}

void sb_width(Scrollback *sb, unsigned cols)
{
    unsigned i, slot;

    if (cols < 1)
        cols = 1;
    if (cols == sb->cols)
        return;
    sb->cols = cols;

    sb->total_rows = 0;
    for (i = 0; i < sb->count; i++) {
        SbLine *ln;
        slot = (sb->head + i) % SB_MAX_LINES;
        ln = &sb->lines[slot];
        ln->rows = ln->len
            ? wrap_rows(sb->blocks[ln->block] + ln->off, ln->len, cols,
                        ln->color)
            : 1;
        sb->total_rows += ln->rows;
    }

    sb->open_tail       = 0;
    sb->open_rows       = 0;
    sb->open_tail_color = sb->open_color;
    open_reflow(sb);
}

unsigned long sb_rows(const Scrollback *sb)
{
    return sb->total_rows + sb->open_rows + 1;
}

unsigned sb_lines(const Scrollback *sb)
{
    return sb->count + 1;
}

/* ---------------------------------------------------------------- */
/* Viewing                                                           */
/* ---------------------------------------------------------------- */

static void view_open_line(SbView *v)
{
    const Scrollback *sb = v->sb;

    sb_wrap_begin(&v->w, sb->open, sb->open_len, sb->cols, sb->open_color);
    v->who = sb->origin;
    v->in_open = 1;
}

static void view_line(SbView *v, unsigned slot)
{
    const Scrollback *sb = v->sb;
    const SbLine *ln = &sb->lines[slot];

    sb_wrap_begin(&v->w, ln->len ? sb->blocks[ln->block] + ln->off : "",
                  ln->len, sb->cols, ln->color);
    v->who = ln->who;
    v->slot = slot;
}

int sb_view(const Scrollback *sb, unsigned long row, SbView *v)
{
    unsigned i, slot;
    SbRow    r;

    v->sb      = sb;
    v->in_open = 0;
    v->done    = 0;

    /* The cached total is the authority on where the end is. Without
       this the skip loop below can land exactly on the last row, having
       consumed it, and report success for a row that is past the end. */
    if (row >= sb_rows(sb)) {
        v->done = 1;
        return 0;
    }

    /* Seek by logical line first: the cached row counts make this a
       walk over lines, not over text. */
    for (i = 0; i < sb->count; i++) {
        slot = (sb->head + i) % SB_MAX_LINES;
        if (row < sb->lines[slot].rows) {
            view_line(v, slot);
            v->left = sb->count - i - 1;
            while (row > 0 && sb_wrap_next(&v->w, &r))
                row--;
            return 1;
        }
        row -= sb->lines[slot].rows;
    }

    /* Whatever is left is in the open line. */
    v->left = 0;
    view_open_line(v);
    while (row > 0 && sb_wrap_next(&v->w, &r))
        row--;
    if (row > 0) {
        v->done = 1;
        return 0;
    }
    return 1;
}

int sb_view_next(SbView *v, SbRow *out)
{
    if (v->done)
        return 0;

    /* An empty logical line still yields one (blank) row from the
       iterator, which is what makes blank lines space one turn from the
       next. So the only job here is stepping on to the following line. */
    for (;;) {
        if (sb_wrap_next(&v->w, out)) {
            out->who = v->who;
            return 1;
        }
        if (v->in_open) {
            v->done = 1;
            return 0;
        }
        if (v->left == 0) {
            view_open_line(v);
        } else {
            view_line(v, (v->slot + 1) % SB_MAX_LINES);
            v->left--;
        }
    }
}
