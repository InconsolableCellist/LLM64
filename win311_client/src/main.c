/*
 * LLM64 for Windows - frame, windows and frame dispatch
 *
 * A 16-bit Windows 3.x client for the LLM64 proxy. It is an MDI
 * application: one frame window with the menu bar, the status strip and
 * the socket, and document windows inside it. Two kinds of document so
 * far - the conversation, an owner-drawn transcript pane over a one-line
 * input box, and a sheet of paper, which is a print job the proxy
 * composed and this program caught instead of sending to a printer. The
 * picture viewer and jukebox join them the same way rather than as loose
 * top-level windows.
 *
 * Both kinds are a View (below): text, a scroll position, and whether it
 * re-flows. The transcript re-flows because it is a conversation; paper
 * does not, because the proxy already laid it out to a printer width and
 * re-wrapping it would be re-typesetting someone else's document.
 *
 * MDI is the period-correct answer and also the one that aged well:
 * Word, Excel and Program Manager all worked this way, and a window
 * full of loose windows is the thing Windows spent the nineties moving
 * away from. Structurally it costs three things and no more:
 * DefFrameProc and DefMDIChildProc instead of DefWindowProc, a
 * MDICLIENT between the frame and its documents, and
 * TranslateMDISysAccel in the message loop.
 *
 * What the spike proved, and this keeps proving (docs/16 section 10):
 *   - Open Watcom builds a working NE binary for Windows 3.x
 *   - it runs under Wine's 16-bit subsystem
 *   - WSAAsyncSelect drives the protocol without ever blocking
 *   - the +0x20 length bias round-trips against the real proxy
 *   - the in-band colour markers render as colour
 *
 * The transcript moved out of this file in Phase 1: it is unwrapped
 * logical lines in far blocks now, wrapped at paint time, so the pane
 * re-flows on a resize and is not bounded by DGROUP. See scroll.h.
 *
 * Deliberately absent, and scheduled: images, MIDI, printing to a real
 * printer DC, and the conversation manager. See win311_client/README.md.
 */

#include <windows.h>
#include <commdlg.h>
#include <mmsystem.h>   /* MCI: the sequencer plays our .MIDs */
#include <string.h>
#include <stdlib.h>
#include "llmport.h"    /* the 16-bit/32-bit seam; brings i86.h on Watcom */
#include "chrome.h"     /* the 3.1 frame, drawn by us */
#include "wire.h"
#include "net.h"
#include "scroll.h"
#include "resource.h"

#define APP_CLASS   "LLM64Main"
#define CONV_CLASS  "LLM64Conv"
#define PIC_CLASS   "LLM64Pic"
#define MUS_CLASS   "LLM64Mus"
#define CHR_CLASS   "LLM64Chr"
#define INV_CLASS   "LLM64Inv"
#define NOTE_CLASS  "LLM64Note"
#define MAP_CLASS   "LLM64Map"
#define PANE_CLASS  "LLM64Pane"
#define APP_TITLE   "LLM64"
#define INI_FILE    "LLM64.INI"

#define ID_PANE     1000
#define ID_INPUT    1001

/* One document's worth of text and where we are looking at it from. The
   transcript is one of these; every sheet of printed paper is another.
   The pane window that draws it keeps a pointer to its View in its extra
   window bytes, so one PaneProc serves them all. */
typedef struct {
    Scrollback sb;
    long       top;         /* first visible display row */
    int        follow;      /* stick to the bottom as text arrives */
    int        wrap;        /* re-flow to the pane, or keep the layout */
    int        margin;      /* left inset, in pixels */
    HWND       pane;        /* the window drawing it, or NULL */
    int        live;        /* the scrollback has been initialised */
} View;

/* Paper. A print job is composed by the proxy and arrives as laid-out
   ASCII. Every sheet used to get an MDI window of its own, which turned
   four /prints into four windows to close; they live in the Notebook
   now - one window, an index down the side, the page beside it. The cap
   is memory honesty, not taste: each View carries a 2 KB open-line
   buffer in DGROUP and its own far blocks on the heap, so six is a
   deliberate number and not a round one. */
#define MAX_PAPER   6

static View     g_conv_view;
static View     g_paper[MAX_PAPER];
static unsigned g_paper_seq;        /* sheets printed, for the titles */

static HWND     g_frame;
static HMENU    g_menubar;   /* the resource menu, detached; chrome draws it */    /* the one top-level window */
static HWND     g_mdi;      /* MDICLIENT, between the frame and its docs */
static HWND     g_conv;     /* the conversation document */
static HWND     g_input;    /* the conversation's input box */
/* One font per combination of bold, italic and underline. SB_ATTR_BOLD,
   _ITALIC and _ULINE are 1, 2 and 4, so the attribute bits index this
   table directly. g_fonts[0] is the plain face and is never NULL; the
   rest may be, and attr_font degrades rather than substituting the
   wrong metrics (see fonts_init). */
#define FONT_VARIANTS 8
static HFONT    g_fonts[FONT_VARIANTS];
static HFONT    g_font;                 /* == g_fonts[0], read constantly */
static int      g_cw = 8, g_ch = 16;    /* character cell */
static LlmOldProc g_old_edit_proc;
static char     g_status[128] = "Not connected.";
/* The proxy composes its own right-hand chrome - place, now playing,
   pictures waiting - and sends it in HINT. It gets its own half of the
   strip rather than overwriting the status, which is what it used to do:
   an empty chrome then wiped whatever the client had just said. */
static char     g_chrome[64];
static char     g_host[64];
static unsigned g_port;
static char     g_ini[160];     /* full path to LLM64.INI */
/* Settings > Pictures: ask the proxy to illustrate every location
   change in an adventure. Off by default and persisted in the INI -
   each picture may be a paid API call, so it is the player's switch. */
static int      g_room_pics;

static unsigned char g_rxbuf[WIRE_MAX_PAYLOAD];
static WireRx        g_rx;
/* Outgoing frame staging buffer. Named for the wire, not the window:
   "frame" means the MDI frame everywhere else in this file. */
static unsigned char g_txframe[WIRE_MAX_PAYLOAD + 8];

/* The print job being received. One at a time: the proxy holds a media
   lock and waits for an ACK per block. */
static int      g_prt_active;
static int      g_prt_slot;
static unsigned g_prt_blocks, g_prt_total;
static int      g_prt_formfeed;

/* ---------------------------------------------------------------- */
/* Colour                                                            */
/* ---------------------------------------------------------------- */

/* Slots the two tables below define: the C64's sixteen, then the
   extended marker's. A proxy whose colour table has outgrown this
   client's will send a slot past the end - a cosmetic miss, and not a
   reason to index off the end of an array (see pal_color). */
#define PAL_SLOTS  32

/* Pepto's C64 palette, the same table the proxy converts images with
   (llm64_proxy/src/imaging.py). Index 0 is black and never used as a
   text colour. */
static const COLORREF g_pal_screen[PAL_SLOTS] = {
    RGB(0x00,0x00,0x00), RGB(0xFF,0xFF,0xFF), RGB(0x68,0x37,0x2B),
    RGB(0x70,0xA4,0xB2), RGB(0x6F,0x3D,0x86), RGB(0x58,0x8D,0x43),
    RGB(0x35,0x28,0x79), RGB(0xB8,0xC7,0x6F), RGB(0x6F,0x4F,0x25),
    RGB(0x43,0x39,0x00), RGB(0x9A,0x67,0x59), RGB(0x44,0x44,0x44),
    RGB(0x6C,0x6C,0x6C), RGB(0x9A,0xD2,0x84), RGB(0x6C,0x5E,0xB5),
    RGB(0x95,0x95,0x95),
    /* Slots 16-31: the extended marker's, and the reason CLIENT_HELLO
       announces rich text. Not on a C64 at any brightness - these are
       what a VGA can say and a VIC-II cannot. Tuned for the dark
       background, so they are lit rather than inked. */
    RGB(0x2E,0xB8,0xB8), RGB(0x50,0x60,0xC8), RGB(0xB8,0x40,0x40),
    RGB(0xA8,0xB0,0x40), RGB(0xE0,0xB8,0x40), RGB(0xE0,0x50,0x60),
    RGB(0xC0,0xA8,0xE8), RGB(0x88,0xC8,0xF0), RGB(0xF0,0x90,0xA8),
    RGB(0x88,0xE0,0xB8), RGB(0xF0,0xA8,0x40), RGB(0x90,0xA0,0xB0),
    RGB(0xC8,0x80,0xC0), RGB(0xE0,0xD0,0xA0), RGB(0x88,0xA8,0x70),
    RGB(0xB0,0xB0,0xB8)
};

/* The same fourteen marker slots as inks on white paper. Not the C64
   colours dimmed: half of them are unreadable on white at any
   brightness - yellow and light green worst of all, and light green is
   the colour every assistant reply arrives in. So each slot keeps its
   *hue* and takes a value that reads as ink. Index 1, the default text
   colour, becomes black; index 13 becomes a dark green, which is what
   makes a reply legible rather than merely present. */
static const COLORREF g_pal_paper[PAL_SLOTS] = {
    RGB(0x00,0x00,0x00), RGB(0x00,0x00,0x00), RGB(0xB0,0x14,0x14),
    RGB(0x00,0x70,0x80), RGB(0x80,0x20,0x90), RGB(0x1C,0x70,0x20),
    RGB(0x18,0x28,0xA8), RGB(0x80,0x70,0x00), RGB(0xB0,0x5A,0x00),
    RGB(0x70,0x44,0x10), RGB(0xC0,0x40,0x38), RGB(0x50,0x50,0x50),
    RGB(0x68,0x68,0x68), RGB(0x00,0x64,0x1E), RGB(0x40,0x40,0xB0),
    RGB(0x78,0x78,0x78),
    /* The same extended slots as INKS. Same hues as the screen table
       above, taken down to a value that reads on white - which is the
       whole reason the wire carries a slot number and not an RGB
       triple: the proxy cannot know which background this is. */
    RGB(0x00,0x68,0x70), RGB(0x20,0x30,0x90), RGB(0x80,0x18,0x18),
    RGB(0x60,0x66,0x10), RGB(0x90,0x6C,0x00), RGB(0xA8,0x18,0x30),
    RGB(0x60,0x48,0x98), RGB(0x18,0x60,0x90), RGB(0xA8,0x40,0x60),
    RGB(0x18,0x78,0x58), RGB(0x98,0x60,0x00), RGB(0x48,0x58,0x68),
    RGB(0x78,0x28,0x70), RGB(0x80,0x68,0x30), RGB(0x40,0x58,0x20),
    RGB(0x58,0x58,0x60)
};


#define THEME_PAPER   0
#define THEME_SCREEN  1

static int       g_theme = THEME_PAPER;
static const COLORREF *g_pal = g_pal_paper;
static COLORREF  g_bg = RGB(0xFF,0xFF,0xFF);

/* Clamped, because the slot arrives off the wire. */
static COLORREF pal_color(unsigned char slot)
{
    slot &= SB_COLOR_MASK;
    return g_pal[slot < PAL_SLOTS ? slot : 1];
}

/* Build the face for every attribute combination, from the plain one.
 *
 * Each variant is MEASURED and kept only if it came back at the same cell
 * width. That check is not paranoia: the pane is a grid the painter
 * positions runs on, and a bold or italic face one pixel wider makes a
 * mixed row drift out of alignment with the rows above it. This is the
 * same test the bold-only version did, applied to seven faces instead
 * of one.
 */
static void fonts_init(HDC hdc)
{
    LOGFONT lf;
    TEXTMETRIC tm;
    unsigned i;

    g_fonts[0] = g_font;
    if (!GetObject(g_font, sizeof(lf), (LPSTR)&lf))
        return;                 /* no template: everything degrades to plain */

    for (i = 1; i < FONT_VARIANTS; i++) {
        LOGFONT v = lf;
        HFONT f;

        v.lfWeight    = (i & SB_ATTR_BOLD) ? FW_BOLD : lf.lfWeight;
        v.lfItalic    = (i & SB_ATTR_ITALIC) ? 1 : 0;
        v.lfUnderline = (i & SB_ATTR_ULINE) ? 1 : 0;
        v.lfWidth     = g_cw;

        f = CreateFontIndirect(&v);
        if (!f)
            continue;
        SelectObject(hdc, f);
        GetTextMetrics(hdc, &tm);
        if (tm.tmAveCharWidth == g_cw && tm.tmHeight == g_ch)
            g_fonts[i] = f;
        else
            DeleteObject(f);
    }
    SelectObject(hdc, g_font);
}

/* The face for a run of text.
 *
 * A heading is NOT a fourth face. A larger one would be the obvious
 * reading of it and it cannot be had: the painter positions every run at
 * a multiple of g_cw and fills to a multiple of g_ch, so a font with
 * different metrics would slide out of the grid and leave the row it
 * shares torn. A heading is therefore bold and underlined, which is what
 * a fixed-pitch 1993 application would have done anyway.
 *
 * Degrading rather than substituting: if the italic face could not be
 * had at the cell width, italic text is drawn upright rather than in
 * something a column wider. Losing the slant is a cosmetic miss; losing
 * the grid is a corrupted pane.
 */
static HFONT attr_font(unsigned char attr)
{
    unsigned idx;

    if (attr & SB_ATTR_HEAD)
        attr |= SB_ATTR_BOLD | SB_ATTR_ULINE;
    idx = attr & (SB_ATTR_BOLD | SB_ATTR_ITALIC | SB_ATTR_ULINE);

    if (g_fonts[idx])
        return g_fonts[idx];
    if (g_fonts[idx & ~SB_ATTR_ITALIC])
        return g_fonts[idx & ~SB_ATTR_ITALIC];
    if (g_fonts[idx & SB_ATTR_BOLD])
        return g_fonts[idx & SB_ATTR_BOLD];
    return g_font;
}
/* Kept alive because WM_CTLCOLOR returns it rather than copying it: the
   brush has to outlive the message. */
static HBRUSH    g_bg_brush;

static void theme_apply(int theme)
{
    HMENU m;
    int i;

    g_theme = theme;
    if (theme == THEME_SCREEN) {
        g_pal = g_pal_screen;
        g_bg  = RGB(0x00,0x00,0x00);
    } else {
        g_pal = g_pal_paper;
        g_bg  = RGB(0xFF,0xFF,0xFF);
    }
    if (g_bg_brush)
        DeleteObject(g_bg_brush);
    g_bg_brush = CreateSolidBrush(g_bg);

    if (g_frame) {
        m = GetMenu(g_frame);
        CheckMenuItem(m, IDM_THEME_PAPER, MF_BYCOMMAND
            | (theme == THEME_PAPER ? MF_CHECKED : MF_UNCHECKED));
        CheckMenuItem(m, IDM_THEME_SCREEN, MF_BYCOMMAND
            | (theme == THEME_SCREEN ? MF_CHECKED : MF_UNCHECKED));
    }
    if (g_conv_view.pane)
        InvalidateRect(g_conv_view.pane, NULL, TRUE);
    for (i = 0; i < MAX_PAPER; i++)
        if (g_paper[i].pane)
            InvalidateRect(g_paper[i].pane, NULL, TRUE);
    if (g_input)
        InvalidateRect(g_input, NULL, TRUE);
}

/* ---------------------------------------------------------------- */
/* Transcript                                                        */
/* ---------------------------------------------------------------- */

static int view_rows(const View *v)
{
    RECT rc;
    if (!v->pane)
        return 1;
    GetClientRect(v->pane, &rc);
    return (rc.bottom / g_ch) > 0 ? (int)(rc.bottom / g_ch) : 1;
}

static int view_cols(const View *v)
{
    RECT rc;
    int c;

    /* A sheet of paper is already laid out by the proxy to a printer
       width; re-flowing it would be re-typesetting someone else's
       document. So paper asks for a width nothing can reach and keeps
       the lines it was sent. */
    if (!v->wrap)
        return 1000;
    if (!v->pane)
        return 40;
    GetClientRect(v->pane, &rc);
    c = (int)((rc.right - v->margin - 2) / g_cw);
    if (c < 10) c = 10;
    return c;
}

/* Total rows, clamped to what a 16-bit scroll bar can express. Only a
   pathologically narrow pane over a full transcript can reach this. */
static long view_total(const View *v)
{
    unsigned long n = sb_rows(&v->sb);
    return n > 32000UL ? 32000L : (long)n;
}

static long view_max_top(const View *v)
{
    long max = view_total(v) - view_rows(v);
    return max < 0 ? 0 : max;
}

static void view_sync_scroll(View *v)
{
    long max = view_max_top(v);

    if (v->top > max) v->top = max;
    if (v->top < 0) v->top = 0;
    if (!v->pane)
        return;
    SetScrollRange(v->pane, SB_VERT, 0, (int)max, FALSE);
    SetScrollPos(v->pane, SB_VERT, (int)v->top, TRUE);
}

static void view_bottom(View *v)
{
    v->top = view_max_top(v);
    v->follow = 1;
    view_sync_scroll(v);
}

/* Every write to a document ends the same way: pin to the bottom and
   repaint. The text is independent of any window, so what arrives while
   the document is closed is kept, not dropped - it is simply there when
   a window is opened on it again. */
static void view_touch(View *v)
{
    view_bottom(v);
    if (v->pane)
        InvalidateRect(v->pane, NULL, FALSE);
}

static void say(unsigned char color, const char *s)
{
    sb_say(&g_conv_view.sb, color, s);
    view_touch(&g_conv_view);
}

/* The input box exists only while a conversation document is open, so
   every use of it has to tolerate its absence. */
static void input_enable(int on)
{
    if (!g_input)
        return;
    EnableWindow(g_input, on ? TRUE : FALSE);
    if (on)
        SetFocus(g_input);
}

static void set_status(const char *s)
{
    lstrcpyn(g_status, s, sizeof(g_status) - 1);
    if (g_frame)
        InvalidateRect(g_frame, NULL, FALSE);
}

/* ---------------------------------------------------------------- */
/* Painting                                                          */
/* ---------------------------------------------------------------- */

/* The markers are still in the text - they are what the runs are split
   on, and the row arrives already knowing the colour and weight in force
   at its first cell, which is what makes a span that survives a wrap
   render the same on both rows.

   Draw one row and everything to the left and right of it, leaving no
   pixel of the row touched twice. That is the whole trick to a pane that
   does not flash: a streamed reply repaints many times a second, and
   filling the pane first and drawing text after means the eye catches
   the blank. Each run is drawn with ETO_OPAQUE, which lays down the
   background and the glyphs in one operation, and only the margins -
   which never hold text - are filled separately. */
static void paint_row(HDC hdc, int x0, int y, int right, const SbRow *r)
{
    unsigned i, run_start = 0, mlen;
    int x = x0, n;
    unsigned char color = r->color;
    unsigned char attr = r->attr;
    RECT rr;

    if (x0 > 0) {
        rr.left = 0; rr.top = y; rr.right = x0; rr.bottom = y + g_ch;
        ExtTextOut(hdc, 0, y, ETO_OPAQUE, &rr, "", 0, NULL);
    }

    /* i == r->len closes the last run, and at that point there is no
       byte to read: a row can end flush against the end of an arena
       block, and in protected mode reading one past it is a fault, not
       a stray byte. The Phase 0 version got away with this because its
       lines were NUL-terminated arrays. */
    for (i = 0; i <= r->len; ) {
        mlen = (i < r->len) ? sb_marker_len(r->text + i, r->len - i) : 0;

        if (i == r->len || mlen) {
            n = (int)(i - run_start);
            if (n > 0) {
                SetTextColor(hdc, pal_color(color));
                SelectObject(hdc, attr_font(attr));
                rr.left = x; rr.top = y;
                rr.right = x + n * g_cw; rr.bottom = y + g_ch;
                ExtTextOut(hdc, x, y, ETO_OPAQUE, &rr,
                           (LPSTR)(r->text + run_start), n, NULL);
                x += n * g_cw;
            }
            if (i == r->len)
                break;
            sb_mark_apply(r->text + i, mlen, r->base, &color, &attr);
            i += mlen;
            run_start = i;
        } else {
            i++;
        }
    }

    /* The rest of the row, past the end of the text. */
    if (x < right) {
        rr.left = x; rr.top = y; rr.right = right; rr.bottom = y + g_ch;
        ExtTextOut(hdc, x, y, ETO_OPAQUE, &rr, "", 0, NULL);
    }
}

/* The View a pane window is looking at, kept in its extra window bytes
   so one window procedure serves the transcript and every sheet. */
static View *pane_view(HWND hwnd)
{
    return (View *)GetWindowLong(hwnd, 0);
}

static void pane_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc;
    SbView it;
    SbRow  r;
    int row, rows;
    View *v = pane_view(hwnd);

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);

    if (!v || !v->live) {
        FillRect(hdc, &rc, g_bg_brush);
        EndPaint(hwnd, &ps);
        return;
    }

    /* Opaque, not transparent: the rows lay down their own background,
       which is what keeps a streaming reply from flashing. */
    SetBkMode(hdc, OPAQUE);
    SetBkColor(hdc, g_bg);
    rows = view_rows(v);
    row = 0;
    if (sb_view(&v->sb, (unsigned long)v->top, &it)) {
        for (; row < rows && sb_view_next(&it, &r); row++)
            paint_row(hdc, v->margin, row * g_ch, rc.right, &r);
    }
    /* Only what is left below the last row, and only once. */
    if (row * g_ch < rc.bottom) {
        RECT tail;
        tail.left = 0; tail.top = row * g_ch;
        tail.right = rc.right; tail.bottom = rc.bottom;
        FillRect(hdc, &tail, g_bg_brush);
    }
    EndPaint(hwnd, &ps);
}

/* ---------------------------------------------------------------- */
/* Paper: taking a sheet, and giving it back                         */
/* ---------------------------------------------------------------- */

static unsigned g_paper_born[MAX_PAPER];
/* The sheet's own first line, kept as its entry in the Notebook's index.
   The proxy composes every /print with its title on line one
   (printdoc.finish), so the index writes itself. */
static char     g_paper_name[MAX_PAPER][40];
static int      g_paper_named[MAX_PAPER];

/* The Notebook holds the sheets; discarding one has to tell it so. */
static void note_drop(int slot);

/* A free slot, or the oldest sheet's - six in the Notebook at once is
   plenty, and the seventh print job is a better use of the memory than
   the first one still is. */
static int paper_slot(void)
{
    int i, oldest = 0;

    for (i = 0; i < MAX_PAPER; i++)
        if (!g_paper[i].live)
            return i;
    for (i = 1; i < MAX_PAPER; i++)
        if (g_paper_born[i] < g_paper_born[oldest])
            oldest = i;
    note_drop(oldest);
    return oldest;
}

/* A job that never reached PRINT_END - the link dropped mid-sheet. Give
   the blocks back rather than holding a slot for a document that will
   never finish. */
static void print_abort(void)
{
    if (!g_prt_active)
        return;
    g_prt_active = 0;
    if (g_paper[g_prt_slot].live)
        note_drop(g_prt_slot);
}

/* ---------------------------------------------------------------- */
/* Protocol                                                          */
/* ---------------------------------------------------------------- */

static void send_frame(unsigned char type, const unsigned char *payload,
                       unsigned len)
{
    unsigned n = wire_frame(g_txframe, type, payload, len);
    if (!net_send(g_txframe, n))
        set_status("Send queue full - the link is stalled.");
}

static void send_text_frame(unsigned char type, const char *text)
{
    unsigned len = (unsigned)lstrlen(text) + 1;   /* NUL is part of it */
    if (len > WIRE_MAX_PAYLOAD)
        return;
    send_frame(type, (const unsigned char *)text, len);
}

/* What this build can render. Kept as a named constant next to the
   sender so the two can never drift: the day the painter learns the
   rich markers, this is the line that changes with it - as it did the
   day the picture window learned to eat a DIB. */
#define OUR_CAPS  (CAP_ZERO_WIDTH_MARKERS | CAP_RICH_TEXT \
                   | CAP_DIB_IMAGES | CAP_MIDI | CAP_STATE_JSON \
                   | CAP_CHAR_SHEET | CAP_MAP_DATA)

/* Introduce ourselves, before anything that could produce text.
 *
 * This is what stops the proxy treating us as a C64, and the first thing
 * it buys is correct SPACING. A C64's marker occupies a screen column,
 * so the proxy swallows the space beside every colour tag to keep the
 * line the same length; our painter draws a marker as nothing at all, so
 * that swallowed space is simply missing - "You see asteel doorahead."
 * CAP_ZERO_WIDTH_MARKERS is the whole fix.
 *
 * Fire-and-forget: there is no reply, matching SET_BAUD. Sent on every
 * connect, so a reconnect through Settings > Server re-announces.
 */
static void send_hello(void)
{
    unsigned char p[16];
    int cols = g_conv ? view_cols(&g_conv_view) : 0;

    /* The pane is resizable, so the width is a runtime fact rather than
       a property of the machine. Zero means "use your default" and is
       the honest answer before the first WM_SIZE. */
    if (cols < 0 || cols > 255)
        cols = 0;

    p[0] = HELLO_VERSION;
    p[1] = (unsigned char)cols;
    p[2] = (unsigned char)(WIRE_MAX_PAYLOAD & 0xFF);
    p[3] = (unsigned char)((WIRE_MAX_PAYLOAD >> 8) & 0xFF);
    p[4] = (unsigned char)(OUR_CAPS & 0xFF);
    p[5] = (unsigned char)((OUR_CAPS >> 8) & 0xFF);
    p[6] = 5;
    p[7] = 'w'; p[8] = 'i'; p[9] = 'n'; p[10] = '1'; p[11] = '6';
    send_frame(MSG_CLIENT_HELLO, p, 12);
}

/* Session toggles, sent after the hello and again whenever one changes.
   Sent unconditionally rather than only-when-on: the proxy's default is
   off, but a reconnect after toggling mid-session must say so
   explicitly or the proxy keeps the stale answer. */
static void send_options(void)
{
    unsigned char p[2];

    p[0] = OPT_ROOM_PICS;
    p[1] = (unsigned char)(g_room_pics ? 1 : 0);
    send_frame(MSG_SET_OPTION, p, 2);
}

/* ---------------------------------------------------------------- */
/* The server-fed menu                                               */
/* ---------------------------------------------------------------- */

/* MENU_LIST: [count][more] then [key][label\0][cmd\0] per entry. A cmd
   beginning with '!' is a local action; anything else is a command to
   send as if it had been typed. The proxy decides what is on the menu,
   so a new server feature appears here with no client rebuild - the one
   idea from the C64's F1 panel worth keeping on any machine. */
static struct {
    char key;
    char label[28];
    char cmd[12];
} g_menu[MAX_MENU];
static int g_menu_count;
static int g_menu_choice;

/* The server-fed menu lives in ONE place now: the Menu dialog (F1 and
   the launcher's first button). It briefly also existed as a
   permanent right-hand panel and then as an "Actions" document - two
   renderings of the same entries read as two features, and the panel
   taxed a 640-wide screen 164 pixels for entries a dialog serves on
   demand. */

/* The launcher: a row of rectangular buttons across the top of the
   frame, one per big window, click to open or close - the way a 1993
   program let you see its rooms without memorizing its menus. Owned by
   the frame like the status strip, because it reports on the desk as a
   whole. */
#define LAUNCH_N 8

static HWND g_launch[LAUNCH_N];
/* Button widths, at file scope because two functions have to agree on
   them: the one that decides how many rows they need and the one that
   places them. */
static const int g_launch_w[LAUNCH_N] =
    { 52, 104, 76, 64, 80, 56, 76, 48 };
/* Eight buttons want 588 pixels, and a 640x480 screen has 632 of them
   inside the frame - the row wraps rather than losing its right-hand end
   off the edge, which is what happened to Map the day it was added. */
static int g_launch_rows = 1;

static int launch_btn_h(void)
{
    return g_ch + 8;
}

static int launch_h(void)
{
    return 3 + g_launch_rows * (launch_btn_h() + 3);
}

/* How many rows the strip needs at this width. A pure function of the
   width, called before the frame divides its height up - the placement
   pass below repeats the same packing. */
static void launch_rows_calc(int width)
{
    int i, x = 4, rows = 1;

    for (i = 0; i < LAUNCH_N; i++) {
        if (x + g_launch_w[i] + 4 > width && x > 4) {
            rows++;
            x = 4;
        }
        x += g_launch_w[i] + 4;
    }
    g_launch_rows = rows;
}

static void launch_create(HWND frame)
{
    static const char *label[LAUNCH_N] =
        { "Menu", "Conversation", "Picture", "Music",
          "Character", "Items", "Notebook", "Map" };
    HINSTANCE inst = LLM_INST(frame);
    int i;

    /* Owner-drawn so a button can show that its window is OPEN: 3.1 has
       no push-like checkbox, so the latched look is ours to draw
       (launch_draw, down with FrameProc). */
    for (i = 0; i < LAUNCH_N; i++)
        g_launch[i] = CreateWindow("BUTTON", label[i],
                                   WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                                   0, 0, 10, 10, frame,
                                   (HMENU)(IDC_LAUNCHBASE + i), inst,
                                   NULL);
}

/* Defined with the frame's layout, but the launcher strip needs it
   first: the buttons sit inside the chrome, not at the window's edge. */
static void frame_content(HWND hwnd, RECT *r);

static void launch_layout(HWND frame)
{
    RECT rc;
    int i, x, y, bh = launch_btn_h();

    frame_content(frame, &rc);
    x = (int)rc.left + 4;
    y = (int)rc.top + 3;
    for (i = 0; i < LAUNCH_N; i++) {
        if (x + g_launch_w[i] + 4 > (int)rc.right && x > (int)rc.left + 4) {
            x = (int)rc.left + 4;
            y += bh + 3;
        }
        if (g_launch[i])
            MoveWindow(g_launch[i], x, y, g_launch_w[i], bh, TRUE);
        x += g_launch_w[i] + 4;
    }
}

/* Defined with the other window layout, but needed as soon as a menu
   arrives: the panel changes how much room the documents get. */
static void frame_layout(HWND hwnd);

static void menu_parse(const unsigned char *p, unsigned len)
{
    unsigned i = 2, s;
    int want;

    if (len < 2)
        return;
    want = p[0];
    g_menu_count = 0;
    while (i < len && g_menu_count < MAX_MENU && g_menu_count < want) {
        char key = (char)p[i++];

        s = i;
        while (i < len && p[i]) i++;
        if (i >= len) break;            /* label not terminated: give up */
        lstrcpyn(g_menu[g_menu_count].label, (const char *)(p + s),
                 sizeof(g_menu[0].label));
        i++;

        s = i;
        while (i < len && p[i]) i++;
        if (i >= len) break;
        lstrcpyn(g_menu[g_menu_count].cmd, (const char *)(p + s),
                 sizeof(g_menu[0].cmd));
        i++;

        g_menu[g_menu_count].key = key;
        g_menu_count++;
    }
}

/* Where a finished sheet goes: the Notebook, opened if it is not on the
   desk yet, with the new sheet selected. Defined with the window. */
static void note_add(int slot);

static void print_begin(const unsigned char *p, unsigned len)
{
    int slot;

    /* A re-sent BEGIN is re-ACKed, not started again - the same rule the
       C64 follows, and for the same reason: the proxy may not have heard
       the first ACK. */
    if (g_prt_active) {
        send_frame(MSG_ACK, NULL, 0);
        return;
    }
    if (len < 2) {
        send_frame(MSG_NAK, NULL, 0);
        return;
    }
    slot = paper_slot();
    if (!sb_init(&g_paper[slot].sb)) {
        send_frame(MSG_NAK, NULL, 0);
        set_status("Not enough memory for another sheet.");
        return;
    }
    g_paper[slot].live   = 1;
    g_paper[slot].wrap   = 0;   /* it is already laid out; leave it be */
    g_paper[slot].margin = g_cw;
    g_paper[slot].top    = 0;
    g_paper[slot].follow = 0;   /* a document opens at the top, not the end */
    g_paper[slot].pane   = NULL;
    sb_width(&g_paper[slot].sb, 1000);
    sb_color(&g_paper[slot].sb, 1);     /* ink: black on paper */
    g_paper_born[slot] = ++g_paper_seq;
    g_paper_name[slot][0] = '\0';
    g_paper_named[slot] = 0;

    g_prt_slot     = slot;
    g_prt_active   = 1;
    g_prt_blocks   = 0;
    g_prt_total    = p[1];
    g_prt_formfeed = (p[0] & 2) ? 1 : 0;
    set_status("Printing...");
    send_frame(MSG_ACK, NULL, 0);
}

/* The sheet's index entry, taken from its first non-blank line as the
   bytes go past. Cheaper and simpler than reading it back out of the
   scrollback afterwards, and the proxy always puts the title there. */
static void paper_name_feed(int slot, char c)
{
    int n = lstrlen(g_paper_name[slot]);

    if (c == '\n' || c == '\r') {
        if (n)                      /* a blank first line is not a title */
            g_paper_named[slot] = 1;
        return;
    }
    if (c < 0x20)
        return;
    if (!n && c == ' ')
        return;                     /* skip the indent, keep the words */
    if (n >= (int)sizeof(g_paper_name[0]) - 1) {
        g_paper_named[slot] = 1;
        return;
    }
    g_paper_name[slot][n] = c;
    g_paper_name[slot][n + 1] = '\0';
}

static void print_data(const unsigned char *p, unsigned len)
{
    char msg[64];
    unsigned i;

    if (!g_prt_active)
        return;             /* a stray block after an abort */
    /* The document arrives as ASCII with 0x0A between lines, which is
       exactly what sb_putc wants. The C64 turns them into 0x0D for the
       IEC bus; nothing here has to. */
    for (i = 0; i < len; i++) {
        if (!g_paper_named[g_prt_slot])
            paper_name_feed(g_prt_slot, (char)p[i]);
        sb_putc(&g_paper[g_prt_slot].sb, (char)p[i]);
    }
    g_prt_blocks++;
    wsprintf(msg, "Printing %u/%u...", g_prt_blocks, g_prt_total);
    set_status(msg);
    send_frame(MSG_ACK, NULL, 0);
}

static void print_end(void)
{
    int slot;
    char msg[80];

    if (!g_prt_active)
        return;
    slot = g_prt_slot;
    g_prt_active = 0;
    /* Close a last line the document did not end with a newline. */
    if (g_paper[slot].sb.open_len > 0)
        sb_newline(&g_paper[slot].sb);
    /* The form feed says "eject the page". There is no page to eject, so
       it is the one flag with nothing to do here. */
    g_paper_named[slot] = 1;        /* whatever we got is the title now */
    note_add(slot);
    wsprintf(msg, "Printed %u lines to the Notebook.",
             (unsigned)sb_lines(&g_paper[slot].sb) - 1);
    set_status(msg);
    send_frame(MSG_ACK, NULL, 0);
}

/* ---------------------------------------------------------------- */
/* Pictures                                                          */
/* ---------------------------------------------------------------- */

/* The proxy generates a scene, keeps the original PNG, and - because
   CLIENT_HELLO claimed CAP_DIB_IMAGES - sends this machine a packed
   8-bit DIB rendered from it (IMG_BEGIN fmt=2, wire.h) instead of the
   C64's 10 KB multicolor blob. A DIB is the one image format a 16-bit
   Windows program decodes natively: one StretchDIBits call to show it,
   one BITMAPFILEHEADER in front of it to save it.

   The transfer is the same offset-tagged bulk stream every C64 media
   transfer uses, except the offset is four bytes (a quarter-megabyte
   DIB laps the 16-bit tag) and the frames are sized to our own buffer
   rather than a 6551's. The blob outgrows a 64 KB segment, so every
   pointer into it is huge. */

static HWND          g_pic_wnd;         /* the persistent picture child */
static HGLOBAL       g_pic_mem;         /* finished DIB, whole */
static unsigned long g_pic_size;
static unsigned      g_pic_w, g_pic_h;
static char          g_pic_title[64];
static HPALETTE      g_pic_hpal;        /* its 256 colours, realizable */

/* The transfer in flight. Separate from the finished picture so a
   failed transfer never takes the picture on screen down with it. */
static HGLOBAL       g_img_mem;
static unsigned long g_img_size, g_img_got;
static unsigned      g_img_w, g_img_h;
static unsigned      g_img_win, g_img_frames;
static int           g_img_active;
static char          g_img_title[64];

/* Default-layout bookkeeping (see layout_default). */
static int g_user_arranged;     /* the user moved a document themselves */
/* What the last default layout was computed FOR: the art's dimensions and
   whether there was a shelf to browse. A layout pass that would land in
   the same place is a layout pass not worth taking - it moves windows,
   and moving windows is what makes a load flicker. */
static unsigned g_pic_laid_w, g_pic_laid_h;
static int      g_pic_laid_shelf;
static int g_in_layout;         /* our own MoveWindows are not "the user" */
static int g_layout_ready;      /* creation-time WM_SIZEs are not either */

/* Where each window was when it last closed. A launcher button is a
   toggle, and a toggle whose window comes back somewhere else has quietly
   thrown away the desk the user arranged: every open used to hand
   WM_MDICREATE the same built-in coordinates it used the first time.
   So the geometry outlives the window.

   Kept in MDI client coordinates, which is exactly what MDICREATESTRUCT
   wants back. Only a *restored* rectangle is worth keeping - reopening a
   window at an icon's 32 pixels, or at a maximized rect that the next
   frame resize invalidates, is not remembering a position. */
enum { DESK_CONV, DESK_PIC, DESK_MUS, DESK_CHR, DESK_INV,
       DESK_NOTE, DESK_MAP, DESK_N };
static struct {
    int x, y, cx, cy;
    int ok;
} g_desk[DESK_N];

static void desk_remember(int which, HWND h)
{
    RECT r;
    POINT tl;

    if (!g_mdi || !h || IsIconic(h) || IsZoomed(h))
        return;
    GetWindowRect(h, &r);
    tl.x = r.left;
    tl.y = r.top;
    ScreenToClient(g_mdi, &tl);
    g_desk[which].x  = tl.x;
    g_desk[which].y  = tl.y;
    g_desk[which].cx = r.right - r.left;
    g_desk[which].cy = r.bottom - r.top;
    g_desk[which].ok = 1;
}

/* Fill in an MDICREATESTRUCT's geometry: where this window was last time
   if we know, the built-in default if it has never been open. */
static void desk_place(int which, MDICREATESTRUCT *mcs,
                       int x, int y, int cx, int cy)
{
    if (g_desk[which].ok) {
        mcs->x  = g_desk[which].x;
        mcs->y  = g_desk[which].y;
        mcs->cx = g_desk[which].cx;
        mcs->cy = g_desk[which].cy;
    } else {
        mcs->x  = x;
        mcs->y  = y;
        mcs->cx = cx;
        mcs->cy = cy;
    }
}

/* The shelf: every picture received this session, so the browser list
   can bring any of them back. Each one is a temp FILE, not a global
   block - thirty 256 KB DIBs is 8 MB, which is more memory than the
   machine this program is for. The one on display is the only one in
   RAM. */
#define MAX_SHELF 32
static struct {
    char          title[64];
    char          path[144];
    unsigned      w, h;
    unsigned long size;         /* 0 = a ghost: title known, bytes not */
    unsigned char srvidx;       /* the /pic <n> that fetches a ghost */
} g_shelf[MAX_SHELF];
static int  g_shelf_count;
static int  g_shelf_cur = -1;   /* index on display */
static HWND g_pic_lb;           /* the browser listbox, in the pic window */
static HWND g_pic_auto;         /* "Illustrate every room", same window */

/* The height of that checkbox strip. A macro rather than a constant
   because g_ch is the font's, and the font is chosen at startup. */
#define PIC_AUTO_H  (g_ch + 8)

/* Ghosts ask the server for their picture; defined with the Music
   window's controls, used here first. */
static void send_command(const char *cmd);

/* Write a global block to an open file in segment-safe bites. The bite
   is 16 KB because 16 K divides 64 K: starting from GlobalLock's offset
   0, every bite ends exactly at or before a segment boundary, and the
   huge increment carries into the next segment between bites. (Watcom
   huge arithmetic does NOT normalize offsets - a bite size that let a
   far pointer run off the segment end would wrap to offset 0, the
   img_data bug.) Returns 0 on a short write. */
static int hfile_write(HFILE f, HGLOBAL mem, unsigned long size)
{
    unsigned char __huge *src;
    unsigned long left = size;
    unsigned chunk;
    int ok = 1;

    src = (unsigned char __huge *)GlobalLock(mem);
    if (!src)
        return 0;
    while (left && ok) {
        chunk = left > 16384UL ? 16384 : (unsigned)left;
        if (_lwrite(f, (LPSTR)src, chunk) != chunk)
            ok = 0;
        src  += chunk;
        left -= chunk;
    }
    GlobalUnlock(mem);
    return ok;
}

/* Store a frame's bytes into a global block at an arbitrary offset,
   splitting at the segment boundary FP_OFF reveals - the shared engine
   of every bulk receive. (The lesson it encodes: Watcom huge
   arithmetic does not normalize offsets, and a far copy running off a
   segment's end wraps to offset 0, which is wherever the header is.) */
static void huge_store(HGLOBAL mem, unsigned long off,
                       const unsigned char far *src, unsigned n)
{
    unsigned char __huge *dst;
    unsigned span;
    unsigned long room;

    dst = (unsigned char __huge *)GlobalLock(mem);
    if (!dst)
        return;
    dst += off;
    while (n) {
        room = 0x10000UL - FP_OFF(dst);
        span = (unsigned long)n < room ? n : (unsigned)room;
        _fmemcpy((void far *)dst, (const void far *)src, span);
        dst += span;    /* huge: carries into the next segment */
        src += span;
        n   -= span;
    }
    GlobalUnlock(mem);
}

/* Rebuild the realizable palette from the DIB's colour table, for the
   256-colour drivers this program is nominally for. On a modern deep
   display RealizePalette is a no-op and none of this matters. */
static void pic_palette(void)
{
    static struct {
        WORD         ver;
        WORD         n;
        PALETTEENTRY pe[256];
    } lp;
    unsigned char far *dib;
    int i;

    if (g_pic_hpal) {
        DeleteObject(g_pic_hpal);
        g_pic_hpal = NULL;
    }
    if (!g_pic_mem)
        return;
    dib = (unsigned char far *)GlobalLock(g_pic_mem);
    if (!dib)
        return;
    lp.ver = 0x300;
    lp.n   = 256;
    for (i = 0; i < 256; i++) {
        /* RGBQUAD stores B,G,R; PALETTEENTRY wants R,G,B. */
        lp.pe[i].peBlue  = dib[40 + i * 4];
        lp.pe[i].peGreen = dib[40 + i * 4 + 1];
        lp.pe[i].peRed   = dib[40 + i * 4 + 2];
        lp.pe[i].peFlags = 0;
    }
    GlobalUnlock(g_pic_mem);
    g_pic_hpal = CreatePalette((LPLOGPALETTE)&lp);
}

static void pic_layout(HWND hwnd);
static void layout_default(void);
/* The INI lives at the bottom of the file, next to WinMain that reads
   it; the picture window's checkbox writes it. */
static void save_ini(void);

/* Open (or refresh) the picture window. Created through the MDI client
   like every document; PicProc records the handle in WM_CREATE because
   this call has not returned yet when the first messages arrive. */
static void pic_open(void)
{
    MDICREATESTRUCT mcs;

    if (g_pic_wnd) {
        /* The browser list appears with the first shelf entry, and only a
           layout pass reveals it. The height the window wants also changes
           with it - and with the aspect of the art, which is not known
           until a picture has actually arrived.

           Only when one of those two things actually CHANGED, though. A
           conversation load sends the whole picture roster, which lands
           here as a stream of shelf updates: relaying out the desk on
           every one of them moved three windows for no reason and made
           the load visibly flicker. */
        if (!g_user_arranged
                && (g_pic_w != g_pic_laid_w || g_pic_h != g_pic_laid_h
                    || (g_shelf_count > 0) != g_pic_laid_shelf))
            layout_default();
        pic_layout(g_pic_wnd);
        InvalidateRect(g_pic_wnd, NULL, TRUE);
        return;
    }
    mcs.szClass = PIC_CLASS;
    mcs.szTitle = "Picture";
    mcs.hOwner  = LLM_INST(g_frame);
    desk_place(DESK_PIC, &mcs, 24, 16, 336, 240);
    mcs.style   = 0;
    mcs.lParam  = 0;
    /* Creation sends the new window its first WM_SIZE, and that must
       not read as the user arranging the desk - it would veto the very
       relayout that is about to place this window. */
    g_in_layout = 1;
    SendMessage(g_mdi, WM_MDICREATE, 0, (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
    /* A picture arriving mid-game must not steal the keyboard: MDI
       activates what it creates, so hand the conversation straight
       back. The picture has nothing to type into anyway. */
    if (g_conv)
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_conv, 0L);
}

static void img_abort(void)
{
    if (g_img_mem) {
        GlobalFree(g_img_mem);
        g_img_mem = NULL;
    }
    g_img_active = 0;
}

/* Park the picture on display onto the shelf as a temp file and put its
   title in the browser list. Failure to park is not failure to show -
   the picture stays on screen either way, it just cannot be brought
   back later. */
/* Do two titles name the same picture? The roster caps titles at 39
   bytes while the BEGIN frame carries up to 63, so compare only what
   both could have said. */
static int shelf_same_title(const char *a, const char *b)
{
    int i;

    for (i = 0; i < 39; i++) {
        if (a[i] != b[i])
            return 0;
        if (!a[i])
            return 1;
    }
    return 1;
}

static void shelf_add(void)
{
    HFILE f;
    OFSTRUCT of;
    int i, slot;

    if (!g_pic_mem)
        return;
    /* A ghost with this title becomes real instead of a duplicate: the
       roster listed the picture, and now its bytes have arrived. */
    slot = -1;
    for (i = 0; i < g_shelf_count; i++)
        if (g_shelf[i].size == 0
                && shelf_same_title(g_shelf[i].title, g_pic_title)) {
            slot = i;
            break;
        }
    if (slot >= 0) {
        if (GetTempFileName(0, "L64", 0, g_shelf[slot].path) == 0)
            return;
        f = _lcreat(g_shelf[slot].path, 0);
        if (f == HFILE_ERROR)
            return;
        if (!hfile_write(f, g_pic_mem, g_pic_size)) {
            _lclose(f);
            OpenFile(g_shelf[slot].path, &of, OF_DELETE);
            g_shelf[slot].path[0] = '\0';
            return;
        }
        _lclose(f);
        g_shelf[slot].w    = g_pic_w;
        g_shelf[slot].h    = g_pic_h;
        g_shelf[slot].size = g_pic_size;
        g_shelf_cur = slot;
        if (g_pic_lb)
            SendMessage(g_pic_lb, LB_SETCURSEL, slot, 0L);
        return;
    }
    if (g_shelf_count == MAX_SHELF) {
        /* Full shelf: the oldest goes, temp file and list entry both. */
        OpenFile(g_shelf[0].path, &of, OF_DELETE);
        for (i = 1; i < MAX_SHELF; i++)
            g_shelf[i - 1] = g_shelf[i];
        g_shelf_count--;
        if (g_pic_lb)
            SendMessage(g_pic_lb, LB_DELETESTRING, 0, 0L);
    }
    /* GetTempFileName with unique=0 creates the file as a side effect,
       which is what reserves the name. */
    if (GetTempFileName(0, "L64", 0,
                        g_shelf[g_shelf_count].path) == 0)
        return;
    f = _lcreat(g_shelf[g_shelf_count].path, 0);
    if (f == HFILE_ERROR)
        return;
    if (!hfile_write(f, g_pic_mem, g_pic_size)) {
        _lclose(f);
        OpenFile(g_shelf[g_shelf_count].path, &of, OF_DELETE);
        set_status("Couldn't keep the picture - temp disk full?");
        return;
    }
    _lclose(f);
    lstrcpy(g_shelf[g_shelf_count].title, g_pic_title);
    g_shelf[g_shelf_count].w      = g_pic_w;
    g_shelf[g_shelf_count].h      = g_pic_h;
    g_shelf[g_shelf_count].size   = g_pic_size;
    g_shelf[g_shelf_count].srvidx = 0;
    g_shelf_cur = g_shelf_count++;
    if (g_pic_lb) {
        SendMessage(g_pic_lb, LB_ADDSTRING, 0,
                    (LONG)(LPSTR)(g_pic_title[0] ? g_pic_title
                                                 : (char *)"(untitled)"));
        SendMessage(g_pic_lb, LB_SETCURSEL, g_shelf_cur, 0L);
    }
}

/* Bring shelf entry idx back on display, from its temp file. */
static void shelf_show(int idx)
{
    HFILE f;
    HGLOBAL mem;
    unsigned char __huge *dst;
    unsigned long left;
    unsigned chunk;
    int ok = 1;

    if (idx < 0 || idx >= g_shelf_count || idx == g_shelf_cur)
        return;
    if (g_shelf[idx].size == 0) {
        /* A ghost: the roster knows the title, the server has the
           bytes. Ask for exactly that picture; when it arrives,
           shelf_add turns this entry real. */
        if (g_shelf[idx].srvidx) {
            char cmd[16];
            wsprintf(cmd, "/pic %d", (int)g_shelf[idx].srvidx);
            send_command(cmd);
        }
        return;
    }
    f = _lopen(g_shelf[idx].path, OF_READ);
    if (f == HFILE_ERROR) {
        set_status("That picture's temp file is gone.");
        return;
    }
    mem = GlobalAlloc(GMEM_MOVEABLE, g_shelf[idx].size);
    if (!mem) {
        _lclose(f);
        set_status("Not enough memory for the picture.");
        return;
    }
    dst = (unsigned char __huge *)GlobalLock(mem);
    left = g_shelf[idx].size;
    while (left && ok) {
        chunk = left > 16384UL ? 16384 : (unsigned)left;
        if ((unsigned)_lread(f, (LPSTR)dst, chunk) != chunk)
            ok = 0;
        dst  += chunk;
        left -= chunk;
    }
    GlobalUnlock(mem);
    _lclose(f);
    if (!ok) {
        GlobalFree(mem);
        set_status("That picture's temp file is damaged.");
        return;
    }
    if (g_pic_mem)
        GlobalFree(g_pic_mem);
    g_pic_mem  = mem;
    g_pic_size = g_shelf[idx].size;
    g_pic_w    = g_shelf[idx].w;
    g_pic_h    = g_shelf[idx].h;
    lstrcpy(g_pic_title, g_shelf[idx].title);
    g_shelf_cur = idx;
    pic_palette();
    if (g_pic_wnd)
        InvalidateRect(g_pic_wnd, NULL, TRUE);
}

/* Exit: the temp files must not outlive the session. */
static void shelf_clear(void)
{
    OFSTRUCT of;
    int i;

    for (i = 0; i < g_shelf_count; i++)
        if (g_shelf[i].path[0])
            OpenFile(g_shelf[i].path, &of, OF_DELETE);
    g_shelf_count = 0;
    g_shelf_cur = -1;
}

/* The conversation's picture roster (PIC_LIST): the shelf follows the
   conversation. Every entry arrives as a ghost - a title and the /pic
   index that fetches it - and the newest one's bytes are already on
   their way behind this frame. An empty roster is a new conversation
   sweeping the desk. */
static void pic_list_frame(const unsigned char *p, unsigned len)
{
    unsigned count, i = 1, j;

    if (len < 1)
        return;
    shelf_clear();
    /* Whatever was on display belonged to the previous conversation. */
    if (g_pic_mem) {
        GlobalFree(g_pic_mem);
        g_pic_mem = NULL;
    }
    if (g_pic_hpal) {
        DeleteObject(g_pic_hpal);
        g_pic_hpal = NULL;
    }
    g_pic_title[0] = '\0';
    g_pic_w = g_pic_h = 0;
    g_pic_size = 0;
    count = p[0];
    while (count-- && i < len && g_shelf_count < MAX_SHELF) {
        g_shelf[g_shelf_count].srvidx = p[i++];
        j = 0;
        while (i < len && p[i]) {
            if (j + 1 < sizeof(g_shelf[0].title))
                g_shelf[g_shelf_count].title[j++] = (char)p[i];
            i++;
        }
        g_shelf[g_shelf_count].title[j] = '\0';
        if (i < len)
            i++;                    /* the NUL */
        g_shelf[g_shelf_count].path[0] = '\0';
        g_shelf[g_shelf_count].size = 0;
        g_shelf[g_shelf_count].w = 0;
        g_shelf[g_shelf_count].h = 0;
        g_shelf_count++;
    }
    if (g_pic_lb) {
        SendMessage(g_pic_lb, LB_RESETCONTENT, 0, 0L);
        for (j = 0; (int)j < g_shelf_count; j++)
            SendMessage(g_pic_lb, LB_ADDSTRING, 0,
                        (LONG)(LPSTR)g_shelf[j].title);
    }
    pic_open();                     /* re-layout: the list came or went */
}

static void img_begin(const unsigned char *p, unsigned len)
{
    unsigned long size;

    /* A re-sent BEGIN means the proxy never heard the first ACK; the
       C64 re-ACKs for the same reason. */
    if (g_img_active) {
        send_frame(MSG_ACK, NULL, 0);
        return;
    }
    if (len < IMG_DIB_HDR || p[0] != IMG_FMT_DIB8) {
        /* fmt 0/1 is a C64 blob: only a proxy that predates
           CLIENT_HELLO would send us one. Refuse rather than render it
           wrong - the NAK makes the proxy report the failure. */
        send_frame(MSG_NAK, NULL, 0);
        set_status("Server sent a C64-format image - proxy too old?");
        return;
    }
    size = (unsigned long)p[7] | ((unsigned long)p[8] << 8)
         | ((unsigned long)p[9] << 16) | ((unsigned long)p[10] << 24);
    /* 40 is an empty header; the cap is a 640x400 DIB with slack, and
       what it really guards is GlobalAlloc against a corrupt length. */
    if (size < 40UL || size > 600000UL) {
        send_frame(MSG_NAK, NULL, 0);
        return;
    }
    g_img_mem = GlobalAlloc(GMEM_MOVEABLE, size);
    if (!g_img_mem) {
        send_frame(MSG_NAK, NULL, 0);
        set_status("Not enough memory for the picture.");
        return;
    }
    g_img_size   = size;
    g_img_got    = 0;
    g_img_w      = p[3] | ((unsigned)p[4] << 8);
    g_img_h      = p[5] | ((unsigned)p[6] << 8);
    g_img_win    = p[1];
    g_img_frames = 0;
    g_img_title[0] = '\0';
    if (len > IMG_DIB_HDR)
        lstrcpyn(g_img_title, (const char far *)(p + IMG_DIB_HDR),
                 sizeof(g_img_title) - 1);
    g_img_active = 1;
    set_status("Receiving picture...");
    send_frame(MSG_ACK, NULL, 0);
}

static void img_data(const unsigned char *p, unsigned len)
{
    unsigned long off;
    unsigned n;

    if (!g_img_active || len < 4)
        return;
    off = (unsigned long)p[0] | ((unsigned long)p[1] << 8)
        | ((unsigned long)p[2] << 16) | ((unsigned long)p[3] << 24);
    n = len - 4;
    if (off >= g_img_size)
        n = 0;
    else if (off + n > g_img_size)
        n = (unsigned)(g_img_size - off);
    if (n) {
        huge_store(g_img_mem, off, p + 4, n);
        g_img_got += n;
    }
    /* The proxy stops and waits for this every g_img_win frames - the
       same flow control the C64 does with its 256-byte bites. */
    if (g_img_win && ++g_img_frames % g_img_win == 0) {
        char msg[48];
        wsprintf(msg, "Receiving picture... %u%%",
                 (unsigned)(g_img_got * 100UL / g_img_size));
        set_status(msg);
        send_frame(MSG_ACK, NULL, 0);
    }
}

static void img_end(void)
{
    char msg[96];

    if (!g_img_active)
        return;
    g_img_active = 0;
    if (g_pic_mem)
        GlobalFree(g_pic_mem);
    g_pic_mem  = g_img_mem;
    g_img_mem  = NULL;
    g_pic_size = g_img_size;
    g_pic_w    = g_img_w;
    g_pic_h    = g_img_h;
    lstrcpy(g_pic_title, g_img_title);
    pic_palette();
    shelf_add();
    pic_open();
    if (g_pic_title[0]) {
        wsprintf(msg, "Picture: %s", (LPSTR)g_pic_title);
        set_status(msg);
    } else {
        set_status("Picture received.");
    }
}

/* ---------------------------------------------------------------- */
/* Music                                                             */
/* ---------------------------------------------------------------- */

/* Not SID. The proxy's CAP_MIDI answer to [[MUSIC:]] is a .MID file
   shipped whole (wire.h); this machine's whole job is to write it to a
   temp file and hand it to MCI's sequencer - synthesis belongs to the
   MIDI Mapper, exactly as a 1993 program would have had it. The Music
   window shows what is playing and offers the three controls that
   matter: Pause, Stop, Next. */

static HWND g_mus_wnd;              /* the controls window, if open */
static HWND g_mus_btn[3];           /* Pause/Resume, Stop, Next */
static HWND g_mus_combo;            /* the mood picker */
static HWND g_mus_play;             /* ...and its Play button */

/* The listener's mood vocabulary (MOOD_LIST), server-fed like the F1
   menu: the library is the proxy's, so its moods are too. */
#define MAX_MOODS 24
static char g_moods[MAX_MOODS][16];
static int  g_mood_count;
static char g_mus_title[44];
static char g_mus_author[36];
static char g_mus_mood[20];
static int  g_mus_state;            /* 0 silent, 1 playing, 2 paused */
static int  g_mus_opened;           /* an MCI alias is open */
static char g_mus_file[144];        /* the tune's temp file */

/* The transfer in flight, separate from what is playing. */
static HGLOBAL       g_mid_mem;
static unsigned long g_mid_size, g_mid_got;
static unsigned      g_mid_win, g_mid_frames;
static int           g_mid_active;
static char          g_mid_title[44];
static char          g_mid_author[36];
static char          g_mid_mood[20];

static void mus_update(void)
{
    if (g_mus_wnd) {
        if (g_mus_btn[0])
            SetWindowText(g_mus_btn[0],
                          g_mus_state == 2 ? "Resume" : "Pause");
        InvalidateRect(g_mus_wnd, NULL, TRUE);
    }
}

static void mus_mci_close(void)
{
    if (g_mus_opened) {
        mciSendString("close llm64mid", NULL, 0, NULL);
        g_mus_opened = 0;
    }
    g_mus_state = 0;
}

/* Local silence - what MUSIC_STOP and app exit want. The proxy's idea
   of what is playing is its own business. */
static void mus_stop(void)
{
    mus_mci_close();
    mus_update();
}

static void mus_play_file(void)
{
    char cmd[200];

    mus_mci_close();
    wsprintf(cmd, "open %s type sequencer alias llm64mid",
             (LPSTR)g_mus_file);
    if (mciSendString(cmd, NULL, 0, NULL) != 0) {
        /* No sequencer device is a machine without a sound setup, not
           an error worth a dialog - the C64 plays on without a SID
           filter too. */
        set_status("MIDI open failed - is a sequencer device installed?");
        return;
    }
    g_mus_opened = 1;
    if (mciSendString("play llm64mid notify", NULL, 0,
                      g_frame) != 0) {
        set_status("MIDI play failed.");
        mus_mci_close();
        return;
    }
    g_mus_state = 1;
    mus_update();
}

static void mid_abort(void)
{
    if (g_mid_mem) {
        GlobalFree(g_mid_mem);
        g_mid_mem = NULL;
    }
    g_mid_active = 0;
}

/* Copy a NUL-terminated field out of the BEGIN payload, advancing the
   cursor past it either way. */
static void mid_field(char *dst, unsigned cap,
                      const unsigned char **q, const unsigned char *end)
{
    unsigned i = 0;

    while (*q < end && **q) {
        if (i + 1 < cap)
            dst[i++] = (char)**q;
        (*q)++;
    }
    dst[i] = '\0';
    if (*q < end)
        (*q)++;                     /* the NUL itself */
}

static void mus_fill_moods(void);

/* MOOD_LIST: the server's mood vocabulary for the picker. */
static void mood_list_frame(const unsigned char *p, unsigned len)
{
    unsigned count, i = 1, j;

    if (len < 1)
        return;
    g_mood_count = 0;
    count = p[0];
    while (count-- && i < len && g_mood_count < MAX_MOODS) {
        j = 0;
        while (i < len && p[i]) {
            if (j + 1 < sizeof(g_moods[0]))
                g_moods[g_mood_count][j++] = (char)p[i];
            i++;
        }
        g_moods[g_mood_count][j] = '\0';
        if (i < len)
            i++;                    /* the NUL */
        if (j)
            g_mood_count++;
    }
    mus_fill_moods();
}

static void mid_begin(const unsigned char *p, unsigned len)
{
    const unsigned char *q, *end;
    unsigned long size;

    if (g_mid_active) {             /* BEGIN resent - first ACK lost */
        send_frame(MSG_ACK, NULL, 0);
        return;
    }
    if (len < 5) {
        send_frame(MSG_NAK, NULL, 0);
        return;
    }
    size = (unsigned long)p[1] | ((unsigned long)p[2] << 8)
         | ((unsigned long)p[3] << 16) | ((unsigned long)p[4] << 24);
    /* 14 bytes is an empty SMF; past 256 KB is not a .MID anyone made. */
    if (size < 14UL || size > 0x40000UL) {
        send_frame(MSG_NAK, NULL, 0);
        return;
    }
    g_mid_mem = GlobalAlloc(GMEM_MOVEABLE, size);
    if (!g_mid_mem) {
        send_frame(MSG_NAK, NULL, 0);
        set_status("Not enough memory for the tune.");
        return;
    }
    g_mid_size   = size;
    g_mid_got    = 0;
    g_mid_win    = p[0];
    g_mid_frames = 0;
    q = p + 5;
    end = p + len;
    mid_field(g_mid_title,  sizeof(g_mid_title),  &q, end);
    mid_field(g_mid_author, sizeof(g_mid_author), &q, end);
    mid_field(g_mid_mood,   sizeof(g_mid_mood),   &q, end);
    g_mid_active = 1;
    send_frame(MSG_ACK, NULL, 0);
}

static void mid_data(const unsigned char *p, unsigned len)
{
    unsigned long off;
    unsigned n;

    if (!g_mid_active || len < 4)
        return;
    off = (unsigned long)p[0] | ((unsigned long)p[1] << 8)
        | ((unsigned long)p[2] << 16) | ((unsigned long)p[3] << 24);
    n = len - 4;
    if (off >= g_mid_size)
        n = 0;
    else if (off + n > g_mid_size)
        n = (unsigned)(g_mid_size - off);
    if (n) {
        huge_store(g_mid_mem, off, p + 4, n);
        g_mid_got += n;
    }
    if (g_mid_win && ++g_mid_frames % g_mid_win == 0)
        send_frame(MSG_ACK, NULL, 0);
}

static void mid_end(void)
{
    OFSTRUCT of;
    HFILE f;
    char msg[112];

    if (!g_mid_active)
        return;
    g_mid_active = 0;
    /* Close before delete: MCI holds the old file open. */
    mus_mci_close();
    if (g_mus_file[0])
        OpenFile(g_mus_file, &of, OF_DELETE);
    if (GetTempFileName(0, "MID", 0, g_mus_file) == 0)
        g_mus_file[0] = '\0';
    f = g_mus_file[0] ? _lcreat(g_mus_file, 0) : HFILE_ERROR;
    if (f == HFILE_ERROR || !hfile_write(f, g_mid_mem, g_mid_size)) {
        if (f != HFILE_ERROR)
            _lclose(f);
        mid_abort();
        set_status("Couldn't keep the tune - temp disk full?");
        return;
    }
    _lclose(f);
    GlobalFree(g_mid_mem);
    g_mid_mem = NULL;
    lstrcpy(g_mus_title,  g_mid_title);
    lstrcpy(g_mus_author, g_mid_author);
    lstrcpy(g_mus_mood,   g_mid_mood);
    mus_play_file();
    wsprintf(msg, "Music: %s (%s)", (LPSTR)g_mus_title,
             (LPSTR)g_mus_author);
    set_status(msg);
}

/* ---------------------------------------------------------------- */
/* The character sheet                                               */
/* ---------------------------------------------------------------- */

/* The adventure's [[STATE:]] block, forwarded verbatim because this
   client claimed CAP_STATE_JSON. The proxy only ever sends the
   NORMALIZED form - json.dumps, compact, ASCII - so the scanner below
   is written against that contract and not against JSON at large:
   double-quoted keys, no whitespace, standard escapes. Values we do
   not know stay unread; keys we know may be absent. The same block is
   re-injected into the system prompt as authoritative game state,
   which is what makes rendering it directly the honest choice: the
   sheet can never disagree with the narrator. */

static struct {
    long hp, maxhp, mana, maxmana, gold, score, xp, level, ac, age;
    int  has_hp, has_mana, has_gold, has_score, has_xp, has_level;
    int  has_ac, has_age;
    char location[64];
    char appearance[200];
    char companions[160];
    char effects[120];          /* poisoned, blessed, on fire... */
    char inv[16][40];
    int  inv_n;
    int  inv_total;             /* what the narrator listed, before the cap */
    int  valid;
} g_sheet;

/* The other half of the sheet, and the half the proxy owns: rolled once
   by chargen.py when the adventure starts and fixed for its whole
   length, so it arrives in its own frame (MSG_CHAR_SHEET) rather than
   being restated by the narrator every turn - which is how race and
   class used to drift. */
static struct {
    char name[40];
    char race[24];
    char cls[24];
    char abil[80];              /* "STR 9  DEX 16  CON 11 ..." - flat */
    char skills[120];
    char spells[120];
    char gear[160];
    long hd;
    int  has_hd;
    int  valid;
} g_static;

static HWND g_chr_wnd;          /* the Character window, if open */
static HWND g_chr_btn;          /* its Refresh button */
static HWND g_inv_wnd;          /* the Inventory window, if open */
static HWND g_inv_lb;

/* The button strip along the bottom of the Character window. */
#define CHR_BTN_H  (g_ch + 12)

/* Walk to the value of "key" at depth 1, or NULL. Tracks strings and
   escapes so an appearance like "a scarred {brace} collector" cannot
   derail the depth count. */
static const char *js_find(const char *j, const char *key)
{
    int depth = 0, instr = 0, esc = 0, matching = 0, ki = 0;
    const char *p;

    for (p = j; *p; p++) {
        if (instr) {
            if (esc) {
                esc = 0;
                matching = 0;
            } else if (*p == '\\') {
                esc = 1;
                matching = 0;
            } else if (*p == '"') {
                instr = 0;
                if (matching && key[ki] == '\0' && p[1] == ':')
                    return p + 2;
                matching = 0;
            } else if (matching) {
                if (key[ki] && (char)key[ki] == *p)
                    ki++;
                else
                    matching = 0;
            }
            continue;
        }
        switch (*p) {
        case '"':
            instr = 1;
            matching = (depth == 1);
            ki = 0;
            break;
        case '{': case '[':
            depth++;
            break;
        case '}': case ']':
            depth--;
            break;
        }
    }
    return NULL;
}

static long js_num(const char *j, const char *key, int *has)
{
    const char *v = js_find(j, key);
    long n = 0;
    int neg = 0;

    *has = 0;
    if (!v)
        return 0;
    if (*v == '-') {
        neg = 1;
        v++;
    }
    if (*v < '0' || *v > '9')
        return 0;
    while (*v >= '0' && *v <= '9')
        n = n * 10 + (*v++ - '0');
    *has = 1;
    return neg ? -n : n;
}

/* Copy a string value out, unescaping what json.dumps emits. Returns
   a pointer just past the closing quote (for the array walker), or
   NULL if v is not a string. */
static const char *js_copy(const char *v, char *dst, unsigned cap)
{
    unsigned i = 0;

    if (!v || *v != '"')
        return NULL;
    v++;
    while (*v && *v != '"') {
        char c = *v++;
        if (c == '\\') {
            if (*v == 'u') {        /* \uXXXX - not worth rendering */
                v += (lstrlen(v) >= 5) ? 5 : lstrlen(v);
                c = '?';
            } else {
                c = (*v == 'n' || *v == 't') ? ' ' : *v;
                if (*v)
                    v++;
            }
        }
        if (i + 1 < cap)
            dst[i++] = c;
    }
    dst[i] = '\0';
    return *v ? v + 1 : v;
}

static void js_str(const char *j, const char *key, char *dst, unsigned cap)
{
    dst[0] = '\0';
    js_copy(js_find(j, key), dst, cap);
}

/* An array of strings: into rows for the inventory, or joined with
   commas for the companions line. */
static int js_strarr(const char *j, const char *key,
                     char rows[][40], int maxrows,
                     char *joined, unsigned joincap)
{
    const char *v = js_find(j, key);
    char item[80];
    int n = 0;
    unsigned ji = 0;

    if (joined)
        joined[0] = '\0';
    if (!v || *v != '[')
        return 0;
    v++;
    while (*v && *v != ']') {
        if (*v == '"') {
            v = js_copy(v, item, sizeof(item));
            if (!v)
                break;
            if (rows && n < maxrows) {
                lstrcpyn(rows[n], item, 40 - 1);
                rows[n][39] = '\0';
            }
            if (joined) {
                unsigned k = 0;
                if (ji && ji + 2 < joincap) {
                    joined[ji++] = ',';
                    joined[ji++] = ' ';
                }
                while (item[k] && ji + 1 < joincap)
                    joined[ji++] = item[k++];
                joined[ji] = '\0';
            }
            n++;
        } else {
            v++;
        }
    }
    return n > maxrows && rows ? maxrows : n;
}

static void sheet_parse(const char *j)
{
    memset(&g_sheet, 0, sizeof(g_sheet));
    if (!j || *j != '{')
        return;
    {
        /* The max is read into its own flag: "maxhp" absent must not
           erase the fact that "hp" arrived. */
        int hmax;
        g_sheet.hp      = js_num(j, "hp",      &g_sheet.has_hp);
        g_sheet.maxhp   = js_num(j, "maxhp",   &hmax);
        if (!hmax)
            g_sheet.maxhp = g_sheet.hp;
        g_sheet.mana    = js_num(j, "mana",    &g_sheet.has_mana);
        g_sheet.maxmana = js_num(j, "maxmana", &hmax);
        if (!hmax)
            g_sheet.maxmana = g_sheet.mana;
    }
    g_sheet.gold    = js_num(j, "gold",    &g_sheet.has_gold);
    g_sheet.score   = js_num(j, "score",   &g_sheet.has_score);
    g_sheet.xp      = js_num(j, "xp",      &g_sheet.has_xp);
    g_sheet.level   = js_num(j, "level",   &g_sheet.has_level);
    g_sheet.ac      = js_num(j, "ac",      &g_sheet.has_ac);
    /* "_age" is the proxy's own key, not the narrator's: turns since the
       model last handed over a state block. Zero means this turn. */
    g_sheet.age     = js_num(j, "_age",    &g_sheet.has_age);
    js_str(j, "location",   g_sheet.location,   sizeof(g_sheet.location));
    js_str(j, "appearance", g_sheet.appearance, sizeof(g_sheet.appearance));
    g_sheet.inv_total = js_strarr(j, "inventory", g_sheet.inv, 16, NULL, 0);
    g_sheet.inv_n = g_sheet.inv_total > 16 ? 16 : g_sheet.inv_total;
    js_strarr(j, "companions", NULL, 0,
              g_sheet.companions, sizeof(g_sheet.companions));
    js_strarr(j, "effects", NULL, 0,
              g_sheet.effects, sizeof(g_sheet.effects));
    /* Every field counts towards "there is a sheet here". The old test
       named four, so a block carrying only gold and a level rendered the
       "no adventure state yet" placeholder over live data. */
    g_sheet.valid = g_sheet.has_hp || g_sheet.has_mana || g_sheet.has_gold
        || g_sheet.has_score || g_sheet.has_xp || g_sheet.has_level
        || g_sheet.has_ac || g_sheet.location[0] || g_sheet.appearance[0]
        || g_sheet.companions[0] || g_sheet.effects[0] || g_sheet.inv_n;
}

/* The static half. Same contract, same flat scanner: the proxy sends
   depth-1 JSON, with the ability scores already laid out as one string
   because a nested object would buy nothing to draw. */
static void chr_static_frame(const char *j);

static void chr_static_parse(const char *j)
{
    memset(&g_static, 0, sizeof(g_static));
    if (!j || *j != '{')
        return;
    js_str(j, "name",  g_static.name,  sizeof(g_static.name));
    js_str(j, "race",  g_static.race,  sizeof(g_static.race));
    js_str(j, "class", g_static.cls,   sizeof(g_static.cls));
    js_str(j, "abil",  g_static.abil,  sizeof(g_static.abil));
    g_static.hd = js_num(j, "hd", &g_static.has_hd);
    js_strarr(j, "skills", NULL, 0, g_static.skills, sizeof(g_static.skills));
    js_strarr(j, "spells", NULL, 0, g_static.spells, sizeof(g_static.spells));
    js_strarr(j, "gear",   NULL, 0, g_static.gear,   sizeof(g_static.gear));
    g_static.valid = g_static.name[0] || g_static.race[0] || g_static.cls[0]
        || g_static.abil[0];
}

/* Refresh whatever sheet windows are open. Defined with the windows
   themselves, called from the wire. */
static void sheet_update(void);
/* The two frames whose windows live further down the file: parse, then
   repaint if the window happens to be open. */
static void chr_static_frame(const char *j);
static void map_frame(const unsigned char *p, unsigned len);

/* ---------------------------------------------------------------- */
/* Conversations: the browser's wire side                            */
/* ---------------------------------------------------------------- */

/* The C64 pages through its conversations in a full-screen module
   (mod_convmgr); here the same four messages feed a dialog. This half
   is the wire: list frames accumulate into g_convs, a load streams
   CONVERSATION_DATA into a cleared transcript, and the dialog half
   (ConvDlgProc, further down with the other dialogs) only ever reads
   what landed here. */

#define MAX_CONVS 16            /* one server page (LIST_PAGE) */
#define WM_CONVS_READY (WM_USER + 40)

static struct {
    unsigned long id;
    unsigned long stamp;
    char          title[40];
} g_convs[MAX_CONVS];
static int  g_conv_count;
static int  g_conv_more_pages;  /* another page exists past this one */
static int  g_conv_page;
static int  g_conv_waiting;     /* 2 = request sent, 1 = frames landing */
static HWND g_convdlg;          /* the open dialog, told when list lands */

/* A conversation restore in progress: the transcript was cleared and
   CONVERSATION_DATA frames are being replayed into it. */
static int           g_load_active;
static unsigned char g_load_role = 0xFF;

/* ACKs the ping did not ask for: delete and star answer with a bare
   ACK, and the default ACK case would announce "link is good" for
   them. A small courtesy counter keeps the status honest. */
static int g_ack_quiet;

static void conv_request_page(int page)
{
    unsigned char p[1];

    g_conv_waiting = 2;
    p[0] = (unsigned char)page;
    send_frame(MSG_LIST_CONVERSATIONS, p, 1);
}

static void conv_list_frame(const unsigned char *p, unsigned len)
{
    unsigned count, more, i = 2, j;

    if (len < 2)
        return;
    count = p[0];
    more  = p[1];
    if (g_conv_waiting == 2) {      /* first frame of a fresh response */
        g_conv_count = 0;
        g_conv_waiting = 1;
    }
    while (count-- && i + 9 <= len && g_conv_count < MAX_CONVS) {
        g_convs[g_conv_count].id =
            (unsigned long)p[i] | ((unsigned long)p[i + 1] << 8)
            | ((unsigned long)p[i + 2] << 16)
            | ((unsigned long)p[i + 3] << 24);
        g_convs[g_conv_count].stamp =
            (unsigned long)p[i + 4] | ((unsigned long)p[i + 5] << 8)
            | ((unsigned long)p[i + 6] << 16)
            | ((unsigned long)p[i + 7] << 24);
        i += 8;
        j = 0;
        while (i < len && p[i]) {
            if (j + 1 < sizeof(g_convs[0].title))
                g_convs[g_conv_count].title[j++] = (char)p[i];
            i++;
        }
        g_convs[g_conv_count].title[j] = '\0';
        if (i < len)
            i++;                    /* the NUL */
        g_conv_count++;
    }
    if (!(more & 1)) {              /* response complete */
        g_conv_more_pages = (more >> 1) & 1;
        g_conv_waiting = 0;
        if (g_convdlg)
            PostMessage(g_convdlg, WM_CONVS_READY, 0, 0L);
    }
}

static void conv_data_frame(const unsigned char *p, unsigned len)
{
    unsigned more, role, base;

    if (len < 4)
        return;
    more = p[1];
    role = p[2];
    if (!g_load_active) {
        /* First frame of the restore: the old transcript makes way. */
        g_load_active = 1;
        g_load_role = 0xFF;
        sb_clear(&g_conv_view.sb);
    }
    base = role & 0x7F;
    if (!(role & 0x80)) {           /* a new block, not a continuation */
        if (g_load_role != 0xFF) {
            sb_newline(&g_conv_view.sb);
            sb_newline(&g_conv_view.sb);
        }
        sb_color(&g_conv_view.sb,
                 base == 0 ? 1 : base == 1 ? 13 : 12);
        g_load_role = (unsigned char)base;
    }
    /* The text is already colorized marker bytes with the proxy's own
       NUL terminator, exactly like a chat chunk. */
    sb_puts(&g_conv_view.sb, (const char *)(p + 3));
    view_touch(&g_conv_view);
    if (!(more & 1)) {              /* restore complete */
        g_load_active = 0;
        sb_newline(&g_conv_view.sb);
        sb_newline(&g_conv_view.sb);
        view_touch(&g_conv_view);
        set_status("Conversation loaded.");
        input_enable(1);
    }
}

static void on_frame(unsigned char type, const unsigned char *p, unsigned len)
{
    char note[160];

    switch (type) {
    case MSG_STATUS:
        if (len > 1) {
            lstrcpyn(note, (const char *)p, sizeof(note) - 1);
            set_status(note);
        }
        break;

    case MSG_CHAT_CHUNK:
        /* [seq][text\0] - seq only matters to a link that can lose
           frames, which TCP is not. Kept in the payload for the C64. */
        if (len > 1) {
            sb_color(&g_conv_view.sb, 13);
            sb_puts(&g_conv_view.sb, (const char *)(p + 1));
            view_touch(&g_conv_view);
        }
        break;

    case MSG_CHAT_DONE:
        sb_newline(&g_conv_view.sb);
        sb_newline(&g_conv_view.sb);
        view_touch(&g_conv_view);
        set_status("Ready.");
        input_enable(1);
        break;

    case MSG_CHAT_ERROR:
        say(2, (const char *)p);
        set_status("Error.");
        input_enable(1);
        break;

    case MSG_NOTICE:
        say(7, (const char *)p);
        break;

    case MSG_ACK:
        if (g_ack_quiet > 0)
            g_ack_quiet--;          /* a delete or a star, not the ping */
        else
            set_status("Proxy answered the ping - link is good.");
        break;

    case MSG_CONVERSATION_LIST:
        conv_list_frame(p, len);
        break;

    case MSG_CONVERSATION_DATA:
        conv_data_frame(p, len);
        break;

    case MSG_MENU_LIST:
        /* The menu changes with the mode, so the panel follows it: enter
           an adventure and Map and Picture of this scene appear on the
           side without a rebuild of anything. */
        menu_parse(p, len);
        break;

    case MSG_HINT:
        /* [flags][pics][chrome\0] - the proxy-composed right-hand status
           text, already laid out by the proxy to 40 characters. */
        if (len > 2)
            lstrcpyn(g_chrome, (const char *)(p + 2), sizeof(g_chrome) - 1);
        else
            g_chrome[0] = '\0';
        if (g_frame)
            InvalidateRect(g_frame, NULL, FALSE);
        break;

    /* Printing. The proxy composes and lays out the document (docs/14)
       and ships it a block at a time, waiting for an ACK on each because
       the C64 has to stop listening to the wire while it talks to the
       printer. Nothing here needs a printer: the sheet is caught in a
       document window instead, which is what the C64 would have called
       paper. Phase 6 adds a real printer DC alongside it. */
    case MSG_PRINT_BEGIN:
        print_begin(p, len);
        break;

    case MSG_PRINT_DATA:
        print_data(p, len);
        break;

    case MSG_PRINT_END:
        print_end();
        break;

    /* A scene illustration, as a DIB because CLIENT_HELLO asked for
       one. It lands in the persistent picture window - the Wasteland
       arrangement, art beside the prose. */
    case MSG_IMG_BEGIN:
        img_begin(p, len);
        break;

    case MSG_IMG_DATA:
        img_data(p, len);
        break;

    case MSG_IMG_END:
        img_end();
        break;

    /* Music, as a .MID because CLIENT_HELLO claimed CAP_MIDI. */
    case MSG_MIDI_BEGIN:
        mid_begin(p, len);
        break;

    case MSG_MIDI_DATA:
        mid_data(p, len);
        break;

    case MSG_MIDI_END:
        mid_end();
        break;

    case MSG_MUSIC_STOP:
        mus_stop();
        set_status("Music off.");
        break;

    case MSG_MOOD_LIST:
        mood_list_frame(p, len);
        break;

    case MSG_PIC_LIST:
        pic_list_frame(p, len);
        break;

    /* The narrator's bookkeeping, for the sheet windows. */
    case MSG_STATE_JSON:
        if (len >= 1 && p[len - 1] == 0) {
            sheet_parse((const char *)p);
            sheet_update();
        }
        break;

    /* The proxy's own half of the sheet: rolled once, fixed for the
       adventure. */
    case MSG_CHAR_SHEET:
        if (len >= 1 && p[len - 1] == 0)
            chr_static_frame((const char *)p);
        break;

    case MSG_MAP_DATA:
        if (len >= 1 && p[len - 1] == 0)
            map_frame(p, len);
        break;

    default:
        wsprintf(note, "[frame 0x%02X, %u bytes]", (int)type, len);
        say(12, note);
        break;
    }
}

static void pump_socket(void)
{
    unsigned char buf[512];
    int n, i;
    unsigned char t;

    for (;;) {
        n = net_recv(buf, sizeof(buf));
        if (n <= 0)
            return;
        for (i = 0; i < n; i++) {
            t = wire_rx_byte(&g_rx, buf[i]);
            if (t == WIRE_CRC_FAIL) {
                set_status("Checksum failure - frame dropped.");
            } else if (t != WIRE_NONE) {
                on_frame(t, g_rx.payload, g_rx.len);
            }
        }
    }
}

static void do_connect(void)
{
    char err[128];
    char msg[200];

    wire_rx_init(&g_rx, g_rxbuf, sizeof(g_rxbuf));
    /* A half-received sheet cannot be finished across a reconnect. */
    print_abort();
    wsprintf(msg, "Connecting to %s:%u...", (LPSTR)g_host, g_port);
    set_status(msg);
    if (!net_connect(g_host, (unsigned short)g_port, err, sizeof(err))) {
        set_status(err);
        say(2, err);
        return;
    }
}

/* Input history lives with the editor keys (EditProc, further down);
   sending is what records a line into it. */
static void hist_push(const char *text);

static void send_input(void)
{
    char text[512];
    int n;

    if (!g_input)
        return;
    n = GetWindowText(g_input, text, sizeof(text) - 1);
    if (n <= 0)
        return;
    text[n] = '\0';
    hist_push(text);                /* C-p brings it back (EditProc) */
    SetWindowText(g_input, "");

    if (net_state() != NET_UP) {
        say(2, "Not connected. Use File > Connect.");
        return;
    }
    say(1, text);
    send_text_frame(MSG_CHAT_REQUEST, text);
    set_status("Waiting for the model...");
    if (g_input)
        EnableWindow(g_input, FALSE);
}

/* ---------------------------------------------------------------- */
/* Windows                                                           */
/* ---------------------------------------------------------------- */

long FAR PASCAL _export PaneProc(HWND hwnd, UINT msg, UINT wParam,
                                 LONG lParam)
{
    int rows;

    View *v = pane_view(hwnd);

    switch (msg) {
    case WM_PAINT:
        pane_paint(hwnd);
        return 0;
    }
    if (!v || !v->live)
        return DefWindowProc(hwnd, msg, wParam, lParam);

    switch (msg) {
    case WM_VSCROLL:
        rows = view_rows(v);
        switch (LLM_SCROLL_CODE(wParam, lParam)) {
        case SB_LINEUP:   v->top--; break;
        case SB_LINEDOWN: v->top++; break;
        case SB_PAGEUP:   v->top -= rows; break;
        case SB_PAGEDOWN: v->top += rows; break;
        case SB_THUMBPOSITION:
        case SB_THUMBTRACK:
            v->top = LLM_SCROLL_POS(wParam, lParam);
            break;
        default: return 0;
        }
        view_sync_scroll(v);
        /* Scrolling back to the bottom re-arms the follow: a reader who
           has paged up stays where they are while a reply streams in. */
        v->follow = (v->top >= view_max_top(v));
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;

    case WM_SIZE:
        /* The whole point of the far-block transcript: a resize re-flows
           what is already on screen, because nothing was ever stored
           wrapped in the first place. Paper is the exception - it keeps
           the layout it was printed with (view_cols). */
        sb_width(&v->sb, (unsigned)view_cols(v));
        if (v->follow)
            view_bottom(v);
        else
            view_sync_scroll(v);
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

/* Attach a pane window to a View. Both document kinds do exactly this,
   and the pane learns which document it is drawing here and nowhere
   else. */
static HWND pane_create(HWND parent, View *v)
{
    HWND p = CreateWindow(PANE_CLASS, NULL,
                          WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_BORDER,
                          0, 0, 10, 10, parent, (HMENU)ID_PANE,
                          LLM_INST(parent),
                          NULL);
    if (!p)
        return NULL;
    SetWindowLong(p, 0, (LONG)v);
    v->pane = p;
    return p;
}

/* --- the input line's emacs fingers --------------------------------

   Parity with the C64's editor: C-b/f/a/e move, M-b/f move by word,
   C-d/k and M-d delete (C-h arrives as 0x08 and the stock EDIT already
   backspaces on it), and C-p/C-n walk the input history - the one
   meaning those keys can have on a single-line box. Control letters
   arrive as WM_CHAR 1..26; the meta keys arrive as WM_SYSCHAR, and
   swallowing those costs Alt+F's menu while the input has focus - F10
   still reaches the menu bar, and emacs fingers were the point. */

#define HIST_N   16
#define HIST_LEN 256

static char g_hist[HIST_N][HIST_LEN];
static int  g_hist_count;
static int  g_hist_browse = -1;     /* -1 = editing a fresh line */
static char g_hist_stash[HIST_LEN]; /* the fresh line, while browsing */

static void hist_push(const char *text)
{
    int i;

    g_hist_browse = -1;
    if (!text[0])
        return;
    /* Repeating the last line must not fill the ring with copies. */
    if (g_hist_count && lstrcmp(g_hist[0], text) == 0)
        return;
    for (i = (g_hist_count < HIST_N ? g_hist_count : HIST_N - 1);
         i > 0; i--)
        lstrcpy(g_hist[i], g_hist[i - 1]);
    lstrcpyn(g_hist[0], text, HIST_LEN - 1);
    g_hist[0][HIST_LEN - 1] = '\0';
    if (g_hist_count < HIST_N)
        g_hist_count++;
}

static int edit_pos(HWND e)
{
    return (int)HIWORD(SendMessage(e, EM_GETSEL, 0, 0L));
}

static void edit_setpos(HWND e, int pos)
{
    SendMessage(e, EM_SETSEL, 0, MAKELONG(pos, pos));
}

static void edit_cut(HWND e, int from, int to)
{
    SendMessage(e, EM_SETSEL, 0, MAKELONG(from, to));
    SendMessage(e, EM_REPLACESEL, 0, (LONG)(LPSTR)"");
}

static int is_wordch(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z')
        || (c >= 'a' && c <= 'z');
}

static int word_left(const char *t, int pos)
{
    while (pos > 0 && !is_wordch(t[pos - 1]))
        pos--;
    while (pos > 0 && is_wordch(t[pos - 1]))
        pos--;
    return pos;
}

static int word_right(const char *t, int n, int pos)
{
    while (pos < n && !is_wordch(t[pos]))
        pos++;
    while (pos < n && is_wordch(t[pos]))
        pos++;
    return pos;
}

static void hist_recall(HWND e, int older)
{
    if (!g_hist_count)
        return;
    if (older) {
        if (g_hist_browse + 1 >= g_hist_count)
            return;                 /* already at the oldest */
        if (g_hist_browse < 0)
            GetWindowText(e, g_hist_stash, sizeof(g_hist_stash) - 1);
        g_hist_browse++;
    } else {
        if (g_hist_browse < 0)
            return;                 /* already on the fresh line */
        g_hist_browse--;
    }
    SetWindowText(e, g_hist_browse < 0 ? g_hist_stash
                                       : g_hist[g_hist_browse]);
    edit_setpos(e, GetWindowTextLength(e));
}

long FAR PASCAL _export EditProc(HWND hwnd, UINT msg, UINT wParam,
                                 LONG lParam)
{
    char t[512];
    int pos, n;

    switch (msg) {
    case WM_CHAR:
        switch (wParam) {
        case VK_RETURN:
            send_input();
            return 0;
        case 1:                     /* C-a: line start */
            edit_setpos(hwnd, 0);
            return 0;
        case 5:                     /* C-e: line end */
            edit_setpos(hwnd, GetWindowTextLength(hwnd));
            return 0;
        case 2:                     /* C-b: back a char */
            pos = edit_pos(hwnd);
            if (pos > 0)
                edit_setpos(hwnd, pos - 1);
            return 0;
        case 6:                     /* C-f: forward a char */
            pos = edit_pos(hwnd);
            if (pos < GetWindowTextLength(hwnd))
                edit_setpos(hwnd, pos + 1);
            return 0;
        case 4:                     /* C-d: delete right */
            pos = edit_pos(hwnd);
            if (pos < GetWindowTextLength(hwnd))
                edit_cut(hwnd, pos, pos + 1);
            return 0;
        case 11:                    /* C-k: kill to end of line */
            pos = edit_pos(hwnd);
            n = GetWindowTextLength(hwnd);
            if (pos < n)
                edit_cut(hwnd, pos, n);
            return 0;
        case 16:                    /* C-p: an older line */
            hist_recall(hwnd, 1);
            return 0;
        case 14:                    /* C-n: back toward the fresh one */
            hist_recall(hwnd, 0);
            return 0;
        case 8:                     /* Backspace - but with Ctrl held,
                                       the modern habit: eat the word.
                                       Some layers deliver Ctrl+BS as 8
                                       with the modifier, others as 127
                                       below; both mean the same. */
            if (!(GetKeyState(VK_CONTROL) & 0x8000))
                break;              /* plain: the EDIT's own backspace */
            /* fall through */
        case 127:                   /* Ctrl+Backspace, the other spelling
                                       - the stock EDIT inserts it as a
                                       box character, helping no one. */
            GetWindowText(hwnd, t, sizeof(t) - 1);
            pos = edit_pos(hwnd);
            if (pos > 0)
                edit_cut(hwnd, word_left(t, pos), pos);
            return 0;
        }
        break;

    case WM_SYSCHAR:
        switch (wParam) {
        case 'b': case 'B':         /* M-b: back a word */
            GetWindowText(hwnd, t, sizeof(t) - 1);
            edit_setpos(hwnd, word_left(t, edit_pos(hwnd)));
            return 0;
        case 'f': case 'F':         /* M-f: forward a word */
            n = GetWindowText(hwnd, t, sizeof(t) - 1);
            edit_setpos(hwnd, word_right(t, n, edit_pos(hwnd)));
            return 0;
        case 'd': case 'D':         /* M-d: delete the word ahead */
            n = GetWindowText(hwnd, t, sizeof(t) - 1);
            pos = edit_pos(hwnd);
            if (pos < n)
                edit_cut(hwnd, pos, word_right(t, n, pos));
            return 0;
        }
        break;

    case WM_KEYDOWN:
        if (wParam == VK_PRIOR || wParam == VK_NEXT) {
            SendMessage(g_conv_view.pane, WM_VSCROLL,
                        wParam == VK_PRIOR ? SB_PAGEUP : SB_PAGEDOWN, 0L);
            return 0;
        }
        break;
    }
    return CallWindowProc(g_old_edit_proc, hwnd, msg, wParam, lParam);
}

/* Inside a conversation document: transcript over input box. The status
   strip is not here - it belongs to the frame, because it reports on the
   link, which is the application's and not any one document's. */
static void conv_layout(HWND hwnd)
{
    RECT rc;
    int inputh = g_ch + 8;
    int paneh;

    GetClientRect(hwnd, &rc);
    paneh = rc.bottom - inputh;
    if (paneh < g_ch) paneh = g_ch;
    if (g_conv_view.pane)
        MoveWindow(g_conv_view.pane, 0, 0, rc.right, paneh, TRUE);
    if (g_input)
        MoveWindow(g_input, 0, paneh, rc.right, inputh, TRUE);
}

/* The default desk: conversation on the left, picture beside it - the
   old text-adventure arrangement, prose with the art in view. Applied
   at startup and re-applied as the frame resizes, but only until the
   user drags a document themselves: after that the desk is theirs, and
   Window > Default Layout is the way to ask for this one back.

   The g_in_layout guard is what tells our own MoveWindows apart from
   the user's - a child's WM_SIZE cannot otherwise know who caused it. */
/* How tall the picture window wants to be at a given width: the art,
   aspect-fitted to that width (pic_paint tops it into the window), the
   caption band under it, the browser list when there is a shelf to
   browse, and the window's own chrome. It used to take the whole column,
   which was mostly black - the art is wider than it is tall. What the
   column no longer needs goes back to the desk. */
static int pic_wanted_h(int w)
{
    /* Until the first picture lands there is no aspect to fit, so assume
       the one the proxy sends (docs/16 section 6.1). */
    long aw = g_pic_w ? (long)g_pic_w : 640;
    long ah = g_pic_h ? (long)g_pic_h : 400;
    int inner = w - 2 * GetSystemMetrics(SM_CXFRAME);
    int h;

    if (inner < 32)
        inner = 32;
    h = (int)((inner * ah) / aw);
    h += g_ch * 2;                          /* two lines of caption */
    h += PIC_AUTO_H;                        /* the checkbox strip */
    if (g_shelf_count)
        h += g_ch * 5 + 4;                  /* the browser list */
    return h + GetSystemMetrics(SM_CYCAPTION)
             + 2 * GetSystemMetrics(SM_CYFRAME);
}

static void layout_default(void)
{
    RECT rc;
    int pw, ph, mw, mh;

    if (!g_mdi)
        return;
    g_in_layout = 1;
    /* A maximized document owns the whole workspace, and MoveWindow on
       it is quietly ignored - restore first. */
    if (g_conv && IsZoomed(g_conv))
        SendMessage(g_mdi, WM_MDIRESTORE, (WPARAM)g_conv, 0L);
    if (g_pic_wnd && IsZoomed(g_pic_wnd))
        SendMessage(g_mdi, WM_MDIRESTORE, (WPARAM)g_pic_wnd, 0L);
    if (g_mus_wnd && IsZoomed(g_mus_wnd))
        SendMessage(g_mdi, WM_MDIRESTORE, (WPARAM)g_mus_wnd, 0L);
    GetClientRect(g_mdi, &rc);
    /* The multiply is long on purpose: 892 pixels x 38 is already past
       what a 16-bit int holds, and the overflow made pw negative - a
       picture window with negative width is an invisible one. The old
       side panel kept the workspace narrow enough to hide this. */
    pw = g_pic_wnd
        ? (int)(((long)(int)rc.right * 38) / 100)
        : 0;
    /* Music tucks into the bottom-right corner: under the picture,
       stealing its column's bottom edge - or over the conversation's
       corner when there is no picture. Three text lines plus the
       button row plus its caption. */
    mh = g_mus_wnd ? g_ch * 6 + 64 : 0;
    if (mh > (int)rc.bottom / 2)
        mh = (int)rc.bottom / 2;
    mw = pw ? pw : 260;
    if (mw > (int)rc.right)
        mw = (int)rc.right;
    /* The picture takes the height its aspect asks for and no more; the
       gap between it and the Music corner is deliberate desk space. */
    ph = pw ? pic_wanted_h(pw) : 0;
    if (ph > (int)rc.bottom - mh)
        ph = (int)rc.bottom - mh;
    if (ph < g_ch * 3)
        ph = g_ch * 3;
    if (g_conv)
        MoveWindow(g_conv, 0, 0, (int)rc.right - pw,
                   (int)rc.bottom, TRUE);
    if (g_pic_wnd)
        MoveWindow(g_pic_wnd, (int)rc.right - pw, 0, pw, ph, TRUE);
    if (g_mus_wnd)
        MoveWindow(g_mus_wnd, (int)rc.right - mw,
                   (int)rc.bottom - mh, mw, mh, TRUE);
    g_in_layout = 0;
    g_layout_ready = 1;
    g_pic_laid_w = g_pic_w;
    g_pic_laid_h = g_pic_h;
    g_pic_laid_shelf = g_shelf_count > 0;
}

/* The frame gives everything except the status strip to the MDI client,
   which is what actually owns the document windows. */
/* Where the application's own furniture goes, once the chrome has taken
   its share off the top and the edges. Everything the frame positions or
   paints works inside this rather than the raw client rect - which, since
   we answer WM_NCCALCSIZE ourselves, is now the whole window. */
static void frame_content(HWND hwnd, RECT *r)
{
    int e = chrome_edge(hwnd);

    GetClientRect(hwnd, r);
    r->left   += e;
    r->right  -= e;
    r->bottom -= e;
    r->top     = chrome_top(hwnd);
}

static void frame_layout(HWND hwnd)
{
    RECT rc;
    int statush = g_ch + 6;
    int h, w;

    if (!g_mdi)
        return;
    frame_content(hwnd, &rc);
    /* The strip's height depends on the width, so it has to be decided
       before the height is divided up. */
    launch_rows_calc((int)(rc.right - rc.left));
    h = (rc.bottom - rc.top) - statush - launch_h();
    if (h < g_ch) h = g_ch;
    /* The documents get everything between the launcher strip and the
       status strip. */
    w = (int)(rc.right - rc.left);
    /* Resizing the workspace resizes any maximized document with it, and
       that WM_SIZE is the frame's doing, not the user's: without this
       guard one drag of the frame border made the desk "arranged" and
       every window that closed afterwards came back at its built-in
       coordinates instead of its own. */
    g_in_layout = 1;
    MoveWindow(g_mdi, rc.left, rc.top + launch_h(), w, h, TRUE);
    g_in_layout = 0;
    launch_layout(hwnd);
}

long FAR PASCAL _export ConvProc(HWND hwnd, UINT msg, UINT wParam,
                                 LONG lParam)
{
    LONG cres;
    HINSTANCE inst;

    /* The 3.1 child caption and border. chrome_child_msg declares the
       non-client area in WM_NCCALCSIZE and paints it in WM_NCPAINT, so
       this window's CLIENT rect is unchanged and none of the layout
       below had to move. */
    if (chrome_child_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        inst = LLM_INST(hwnd);
        pane_create(hwnd, &g_conv_view);
        g_input = CreateWindow("EDIT", "",
                               WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL,
                               0, 0, 10, 10, hwnd, (HMENU)ID_INPUT, inst, NULL);
        SendMessage(g_input, WM_SETFONT, (WPARAM)g_font, 0L);
        g_old_edit_proc = (LlmOldProc)GetWindowLong(g_input, GWL_WNDPROC);
        SetWindowLong(g_input, GWL_WNDPROC, (LONG)EditProc);
        break;

    case WM_SIZE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        conv_layout(hwnd);
        break;

    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        break;

    case WM_MDIACTIVATE:
        /* Typing should land in the document you just clicked on, not
           wherever the focus happened to be. */
        if (LLM_MDI_ACTIVE(wParam, lParam, hwnd))
            SetFocus(g_input);
        break;

    /* The input box has to follow the theme, or the Screen palette
       leaves a white box glued under a black transcript. The brush is
       returned, not copied, which is why it is a global that outlives
       the message.

       Win16 has one WM_CTLCOLOR for every control and says which kind
       it is in HIWORD(lParam); Win32 split it into a message per class
       and never sends the old one. */
#ifdef __WATCOMC__
    case WM_CTLCOLOR:
        if (HIWORD(lParam) != CTLCOLOR_EDIT)
            break;
#else
    case WM_CTLCOLOREDIT:
#endif
        SetTextColor((HDC)wParam, g_pal[1]);
        SetBkColor((HDC)wParam, g_bg);
        return (LONG)g_bg_brush;

    case WM_SETFOCUS:
        if (g_input)
            SetFocus(g_input);
        return 0;

    case WM_DESTROY:
        /* A document window can be closed - Ctrl+F4, the close box, the
           system menu. Forget its children rather than leaving handles
           that outlive the windows they name: an arriving CHAT_CHUNK
           would otherwise paint into a window that no longer exists.
           Window > New Conversation Window brings it back - in the
           place it was closed from, which is what desk_remember is
           for. */
        desk_remember(DESK_CONV, hwnd);
        g_conv = NULL;
        g_conv_view.pane = NULL;
        g_input = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* ---------------------------------------------------------------- */
/* The Notebook                                                      */
/* ---------------------------------------------------------------- */

/* Everything the proxy has printed this session, in one window: an index
   of sheets down the left, the selected page beside it. The proxy has
   already composed and laid each document out to a printer width
   (docs/14), so the page half's whole job is to hold still and be read -
   no input box, no re-flow, a margin, and the text exactly where the
   printer would have put it.

   One window rather than one per sheet, which is what /print used to do:
   four printouts were four windows to close, and none of them said what
   was on any of the others. */

static HWND g_note_wnd;
static HWND g_note_lb;          /* the index */
static HWND g_note_pane;        /* the page, re-made as the index moves */
static int  g_note_cur = -1;    /* the sheet on show, or -1 for none */

#define NOTE_INDEX_W  132

static void note_layout(HWND hwnd)
{
    RECT rc;
    int lw;

    GetClientRect(hwnd, &rc);
    lw = NOTE_INDEX_W;
    if (lw > (int)rc.right / 2)
        lw = (int)rc.right / 2;
    if (g_note_lb)
        MoveWindow(g_note_lb, 0, 0, lw, rc.bottom, TRUE);
    if (g_note_pane)
        MoveWindow(g_note_pane, lw, 0, (int)rc.right - lw, rc.bottom, TRUE);
}

/* Rebuild the index. Each row carries its slot in its item data, so the
   row order is free to be anything and the empty state can be a row. */
static void note_fill(void)
{
    int i, rows = 0, sel = -1;

    if (!g_note_lb)
        return;
    SendMessage(g_note_lb, LB_RESETCONTENT, 0, 0L);
    for (i = 0; i < MAX_PAPER; i++) {
        char row[64];
        int r;

        if (!g_paper[i].live)
            continue;
        wsprintf(row, "%u. %s", g_paper_born[i],
                 g_paper_name[i][0] ? (LPSTR)g_paper_name[i]
                                    : (LPSTR)"(untitled)");
        r = (int)SendMessage(g_note_lb, LB_ADDSTRING, 0, (LONG)(LPSTR)row);
        if (r < 0)
            continue;
        SendMessage(g_note_lb, LB_SETITEMDATA, r, (LONG)i);
        if (i == g_note_cur)
            sel = r;
        rows++;
    }
    if (!rows) {
        SendMessage(g_note_lb, LB_ADDSTRING, 0,
                    (LONG)(LPSTR)"(nothing printed)");
        SendMessage(g_note_lb, LB_SETITEMDATA, 0, (LONG)-1);
    } else if (sel >= 0) {
        SendMessage(g_note_lb, LB_SETCURSEL, sel, 0L);
    }
}

/* Show a sheet, or nothing. The pane is made and destroyed with the
   selection rather than rebound in place: a View owns exactly one pane
   pointer, and a pane left pointing at a freed scrollback is the one
   mistake here that crashes rather than merely looking wrong. */
static void note_show(int slot)
{
    if (!g_note_wnd)
        return;
    if (g_note_pane) {
        if (g_note_cur >= 0 && g_note_cur < MAX_PAPER
                && g_paper[g_note_cur].pane == g_note_pane)
            g_paper[g_note_cur].pane = NULL;
        DestroyWindow(g_note_pane);
        g_note_pane = NULL;
    }
    g_note_cur = -1;
    if (slot >= 0 && slot < MAX_PAPER && g_paper[slot].live) {
        g_note_cur = slot;
        g_paper[slot].top = 0;      /* a page opens at its top */
        g_note_pane = pane_create(g_note_wnd, &g_paper[slot]);
    }
    note_layout(g_note_wnd);
    InvalidateRect(g_note_wnd, NULL, TRUE);
}

/* Throw a sheet away. Eviction and "clear it out" both come here, and
   both have to get the pane off it first. */
static void note_drop(int slot)
{
    if (slot < 0 || slot >= MAX_PAPER)
        return;
    if (g_note_cur == slot)
        note_show(-1);
    if (g_paper[slot].live)
        sb_free(&g_paper[slot].sb);
    g_paper[slot].live = 0;
    g_paper[slot].pane = NULL;
    g_paper_name[slot][0] = '\0';
    g_paper_named[slot] = 0;
    if (g_prt_active && g_prt_slot == slot)
        g_prt_active = 0;
    note_fill();
}

static void note_clear(void)
{
    int i;

    for (i = 0; i < MAX_PAPER; i++)
        if (g_paper[i].live)
            note_drop(i);
}

static void note_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc = BeginPaint(hwnd, &ps);
    RECT rc;
    int lw;

    /* Only the part no child covers - which with no sheet selected is the
       whole right-hand side, and is where the empty state goes. */
    GetClientRect(hwnd, &rc);
    lw = NOTE_INDEX_W;
    if (lw > (int)rc.right / 2)
        lw = (int)rc.right / 2;
    rc.left = lw;
    FillRect(hdc, &rc, GetStockObject(LTGRAY_BRUSH));
    if (!g_note_pane) {
        rc.top += g_ch * 2;
        SetBkMode(hdc, TRANSPARENT);
        SelectObject(hdc, GetStockObject(SYSTEM_FONT));
        SetTextColor(hdc, RGB(0x40, 0x40, 0x40));
        DrawText(hdc,
                 "Nothing printed yet.\n\nAsk for a printout - "
                 "\"/print the letter\" - and every sheet is filed here.",
                 -1, &rc, DT_CENTER | DT_WORDBREAK | DT_NOPREFIX);
    }
    EndPaint(hwnd, &ps);
}

long FAR PASCAL _export NoteProc(HWND hwnd, UINT msg, UINT wParam,
                                 LONG lParam)
{
    LONG cres;
    int i;

    /* The 3.1 child caption and border. chrome_child_msg declares the
       non-client area in WM_NCCALCSIZE and paints it in WM_NCPAINT, so
       this window's CLIENT rect is unchanged and none of the layout
       below had to move. */
    if (chrome_child_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        g_note_wnd = hwnd;
        g_note_lb = CreateWindow("LISTBOX", NULL,
                                 WS_CHILD | WS_VISIBLE | WS_BORDER
                                 | WS_VSCROLL | LBS_NOTIFY,
                                 0, 0, 10, 10, hwnd, (HMENU)ID_NOTELIST,
                                 LLM_INST(hwnd),
                                 NULL);
        SendMessage(g_note_lb, WM_SETFONT, (WPARAM)g_font, 0L);
        /* The sheets outlive this window, so reopening it finds them
           again - the picture shelf's rule, applied to paper. */
        note_fill();
        if (g_note_cur < 0) {
            for (i = MAX_PAPER - 1; i >= 0; i--)
                if (g_paper[i].live)
                    break;
            note_show(i);           /* the newest, or -1 if there are none */
        } else {
            note_show(g_note_cur);
        }
        break;

    case WM_PAINT:
        note_paint(hwnd);
        return 0;

    case WM_COMMAND:
        if (LLM_CMD_ID(wParam, lParam) == ID_NOTELIST
            && LLM_CMD_NOTIFY(wParam, lParam) == LBN_SELCHANGE) {
            int r = (int)SendMessage(g_note_lb, LB_GETCURSEL, 0, 0L);
            if (r >= 0)
                note_show((int)SendMessage(g_note_lb, LB_GETITEMDATA,
                                           r, 0L));
            return 0;
        }
        break;

    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        note_layout(hwnd);
        break;

    case WM_MDIACTIVATE:
        /* Paper has nothing to type into, so activating the Notebook must
           not leave the keyboard on the conversation's input box - give
           the focus to the page, where PgUp and PgDn work. */
        if (LLM_MDI_ACTIVE(wParam, lParam, hwnd) && g_note_pane)
            SetFocus(g_note_pane);
        break;

    case WM_DESTROY:
        desk_remember(DESK_NOTE, hwnd);
        /* The pane dies with its parent; unbind it so nothing later paints
           through a stale handle. The SHEETS stay - closing the index is
           not throwing the paper away. */
        if (g_note_cur >= 0 && g_note_cur < MAX_PAPER)
            g_paper[g_note_cur].pane = NULL;
        g_note_pane = NULL;
        g_note_lb = NULL;
        g_note_wnd = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* ---------------------------------------------------------------- */
/* The Map                                                           */
/* ---------------------------------------------------------------- */

/* The adventure's geography, drawn rather than printed. The proxy keeps
   the authoritative map (rooms, one-way edges, what has only been heard
   of) and already renders an ASCII version for /print and for the C64;
   this client claims CAP_MAP_DATA and gets the structure instead, in one
   MAP_DATA frame:

       M<turn>\t<cols>\t<rows>
       R<num>\t<gx>\t<gy>\t<flags>\t<name>     flags: 1 visited, 2 you
       E<a>\t<b>\t<dir>\t<flags>               dir n s e w u d or -
       X<hidden>                               rooms that did not fit

   Drawn the way a 1993 game drew a map: boxes ruled on paper, the room
   number in each, ink lines between them, and no gradient anywhere. */

#define MAP_ROOMS  40
#define MAP_EDGES  72
#define MAP_NAME   21

static HWND g_map_wnd;
static struct {
    unsigned char num, gx, gy, flags;
    char          name[MAP_NAME];
} g_map_room[MAP_ROOMS];
static struct {
    unsigned char a, b, flags;
    char          dir;
} g_map_edge[MAP_EDGES];
static int      g_map_nrooms, g_map_nedges;
static int      g_map_cols, g_map_rows, g_map_hidden;
static unsigned g_map_turn;
static int      g_map_valid;        /* a frame has arrived this session */

/* One tab-separated field, copied out and NUL-terminated. Returns where
   the next field starts, or NULL at the end of the line. */
static const char *map_field(const char *p, char *out, int max)
{
    int n = 0;

    while (*p && *p != '\t' && *p != '\n') {
        if (n < max - 1)
            out[n++] = *p;
        p++;
    }
    out[n] = '\0';
    if (*p == '\t')
        return p + 1;
    return NULL;
}

static int map_int(const char *s)
{
    int v = 0;

    while (*s >= '0' && *s <= '9')
        v = v * 10 + (*s++ - '0');
    return v;
}

static void map_parse(const unsigned char *p, unsigned len)
{
    const char *s = (const char *)p;
    const char *end = s + len;
    char f[64];

    g_map_nrooms = g_map_nedges = 0;
    g_map_cols = g_map_rows = g_map_hidden = 0;
    g_map_turn = 0;
    g_map_valid = 1;

    while (s < end && *s) {
        char kind = *s++;
        const char *next = s;

        switch (kind) {
        case 'M':
            next = map_field(s, f, sizeof(f));
            g_map_turn = (unsigned)map_int(f);
            if (next) {
                next = map_field(next, f, sizeof(f));
                g_map_cols = map_int(f);
            }
            if (next) {
                next = map_field(next, f, sizeof(f));
                g_map_rows = map_int(f);
            }
            break;

        case 'R':
            if (g_map_nrooms < MAP_ROOMS) {
                int i = g_map_nrooms;
                next = map_field(s, f, sizeof(f));
                g_map_room[i].num = (unsigned char)map_int(f);
                if (next) next = map_field(next, f, sizeof(f));
                g_map_room[i].gx = (unsigned char)map_int(f);
                if (next) next = map_field(next, f, sizeof(f));
                g_map_room[i].gy = (unsigned char)map_int(f);
                if (next) next = map_field(next, f, sizeof(f));
                g_map_room[i].flags = (unsigned char)map_int(f);
                if (next) next = map_field(next, g_map_room[i].name,
                                           MAP_NAME);
                else g_map_room[i].name[0] = '\0';
                g_map_nrooms++;
            }
            break;

        case 'E':
            if (g_map_nedges < MAP_EDGES) {
                int i = g_map_nedges;
                next = map_field(s, f, sizeof(f));
                g_map_edge[i].a = (unsigned char)map_int(f);
                if (next) next = map_field(next, f, sizeof(f));
                g_map_edge[i].b = (unsigned char)map_int(f);
                if (next) next = map_field(next, f, sizeof(f));
                g_map_edge[i].dir = f[0] ? f[0] : '-';
                if (next) next = map_field(next, f, sizeof(f));
                g_map_edge[i].flags = (unsigned char)map_int(f);
                g_map_nedges++;
            }
            break;

        case 'X':
            map_field(s, f, sizeof(f));
            g_map_hidden = map_int(f);
            break;
        }
        /* Whatever the line was, skip to the start of the next one. */
        while (s < end && *s && *s != '\n')
            s++;
        if (s < end && *s == '\n')
            s++;
    }
}

static void map_frame(const unsigned char *p, unsigned len)
{
    map_parse(p, len);
    if (g_map_wnd)
        InvalidateRect(g_map_wnd, NULL, TRUE);
}

/* Where a room's box lands, in client pixels. */
static void map_box(const RECT *area, int gx, int gy, RECT *out)
{
    int cw = (area->right - area->left) / (g_map_cols > 0 ? g_map_cols : 1);
    int chh = (area->bottom - area->top) / (g_map_rows > 0 ? g_map_rows : 1);
    int px = 3, py = 3;

    if (cw < 12) cw = 12;
    if (chh < 10) chh = 10;
    if (cw < 30) px = 1;
    if (chh < 24) py = 1;
    out->left   = area->left + gx * cw + px;
    out->top    = area->top + gy * chh + py;
    out->right  = area->left + (gx + 1) * cw - px;
    out->bottom = area->top + (gy + 1) * chh - py;
}

static int map_find(int num)
{
    int i;

    for (i = 0; i < g_map_nrooms; i++)
        if (g_map_room[i].num == (unsigned char)num)
            return i;
    return -1;
}

static void map_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc, area, box;
    HPEN ink, dotted, old_pen;
    HBRUSH paper, fill;
    HFONT old_font;
    char line[80];
    int i, top;

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);

    /* Paper first, and the same paper in either theme: a map is a map. */
    paper = CreateSolidBrush(RGB(0xF4, 0xEC, 0xD8));
    FillRect(hdc, &rc, paper);
    DeleteObject(paper);

    ink = CreatePen(PS_SOLID, 1, RGB(0x20, 0x20, 0x20));
    dotted = CreatePen(PS_DOT, 1, RGB(0x60, 0x60, 0x60));
    old_pen = SelectObject(hdc, ink);
    old_font = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, RGB(0x20, 0x20, 0x20));

    /* The heading, and the rule under it. */
    if (!g_map_valid || g_map_nrooms == 0) {
        RECT tr = rc;
        tr.top += g_ch * 2;
        DrawText(hdc, g_map_valid
                 ? "No ground covered yet.\n\nThe map fills in as you "
                   "explore."
                 : "No map yet.\n\nStart an adventure and the places you "
                   "visit are drawn here.",
                 -1, &tr, DT_CENTER | DT_WORDBREAK | DT_NOPREFIX);
        SelectObject(hdc, old_font);
        SelectObject(hdc, old_pen);
        DeleteObject(ink);
        DeleteObject(dotted);
        EndPaint(hwnd, &ps);
        return;
    }

    if (g_map_hidden)
        wsprintf(line, "%d places, turn %u  (%d off the edge)",
                 g_map_nrooms, g_map_turn, g_map_hidden);
    else
        wsprintf(line, "%d places, turn %u", g_map_nrooms, g_map_turn);
    TextOut(hdc, 6, 3, line, lstrlen(line));
    top = g_ch + 6;
    MoveTo(hdc, 4, top - 2);
    LineTo(hdc, (int)rc.right - 4, top - 2);

    area.left = 4;
    area.top = top;
    area.right = (int)rc.right - 4;
    area.bottom = (int)rc.bottom - 4;

    /* Corridors under the rooms, so a box always wins the overlap. */
    for (i = 0; i < g_map_nedges; i++) {
        int a = map_find(g_map_edge[i].a);
        int b = map_find(g_map_edge[i].b);
        RECT ra, rb;

        if (a < 0 || b < 0)
            continue;
        map_box(&area, g_map_room[a].gx, g_map_room[a].gy, &ra);
        map_box(&area, g_map_room[b].gx, g_map_room[b].gy, &rb);
        /* One-way passages and stairs are dotted: the eye reads a dotted
           line as "not the same as the others", which is all the
           distinction a 1993 map ever made. */
        SelectObject(hdc, (g_map_edge[i].flags & 1)
                     || g_map_edge[i].dir == 'u'
                     || g_map_edge[i].dir == 'd' ? dotted : ink);
        MoveTo(hdc, (ra.left + ra.right) / 2, (ra.top + ra.bottom) / 2);
        LineTo(hdc, (rb.left + rb.right) / 2, (rb.top + rb.bottom) / 2);
    }
    SelectObject(hdc, ink);

    for (i = 0; i < g_map_nrooms; i++) {
        int here = (g_map_room[i].flags & 2) != 0;
        int visited = (g_map_room[i].flags & 1) != 0;
        char num[8];

        map_box(&area, g_map_room[i].gx, g_map_room[i].gy, &box);
        if (box.right <= box.left || box.bottom <= box.top)
            continue;
        /* Where you are is filled; somewhere only heard about is left
           empty with a dotted rule; everywhere else is plain paper. */
        fill = CreateSolidBrush(here ? RGB(0x20, 0x20, 0x20)
                                     : RGB(0xFF, 0xFB, 0xEE));
        FillRect(hdc, &box, fill);
        DeleteObject(fill);
        SelectObject(hdc, visited ? ink : dotted);
        MoveTo(hdc, box.left, box.top);
        LineTo(hdc, box.right - 1, box.top);
        LineTo(hdc, box.right - 1, box.bottom - 1);
        LineTo(hdc, box.left, box.bottom - 1);
        LineTo(hdc, box.left, box.top);
        SelectObject(hdc, ink);

        SetTextColor(hdc, here ? RGB(0xFF, 0xFB, 0xEE)
                               : RGB(0x20, 0x20, 0x20));
        wsprintf(num, "%d", (int)g_map_room[i].num);
        TextOut(hdc, box.left + 3, box.top + 1, num, lstrlen(num));
        /* The name only if there is room for it under the number - a
           label that does not fit is worse than no label. */
        if (box.bottom - box.top >= g_ch * 2 + 2
                && box.right - box.left > 40) {
            RECT nr = box;
            nr.left += 3;
            nr.right -= 2;
            nr.top += g_ch;
            DrawText(hdc, g_map_room[i].name, -1, &nr,
                     DT_WORDBREAK | DT_NOPREFIX | DT_NOCLIP);
        }
    }
    SetTextColor(hdc, RGB(0x20, 0x20, 0x20));

    SelectObject(hdc, old_font);
    SelectObject(hdc, old_pen);
    DeleteObject(ink);
    DeleteObject(dotted);
    EndPaint(hwnd, &ps);
}

long FAR PASCAL _export MapProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    LONG cres;
    /* The 3.1 child caption and border. chrome_child_msg declares the
       non-client area in WM_NCCALCSIZE and paints it in WM_NCPAINT, so
       this window's CLIENT rect is unchanged and none of the layout
       below had to move. */
    if (chrome_child_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        g_map_wnd = hwnd;
        break;

    case WM_PAINT:
        map_paint(hwnd);
        return 0;

    case WM_ERASEBKGND:
        return 1;               /* map_paint covers every pixel */

    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        InvalidateRect(hwnd, NULL, TRUE);
        break;

    case WM_DESTROY:
        desk_remember(DESK_MAP, hwnd);
        g_map_wnd = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* ---------------------------------------------------------------- */
/* The picture window                                                */
/* ---------------------------------------------------------------- */

/* Rows the browser list takes from the bottom of the picture window,
   above the checkbox strip. Zero until there is something to browse - a
   picture on its own deserves the whole window. */
static int pic_list_h(HWND hwnd)
{
    RECT rc;
    int h;

    if (!g_shelf_count)
        return 0;
    GetClientRect(hwnd, &rc);
    h = g_ch * 5 + 4;
    if (h > (int)rc.bottom / 2)
        h = (int)rc.bottom / 2;
    return h;
}

static void pic_layout(HWND hwnd)
{
    RECT rc;
    int lh = pic_list_h(hwnd);
    int ah;

    if (!g_pic_lb)
        return;
    GetClientRect(hwnd, &rc);
    ah = PIC_AUTO_H;
    if (ah > (int)rc.bottom)
        ah = (int)rc.bottom;
    if (g_pic_auto)
        MoveWindow(g_pic_auto, 4, rc.bottom - ah, rc.right - 8, ah - 2,
                   TRUE);
    if (lh) {
        MoveWindow(g_pic_lb, 0, rc.bottom - ah - lh, rc.right, lh, TRUE);
        ShowWindow(g_pic_lb, SW_SHOW);
    } else {
        ShowWindow(g_pic_lb, SW_HIDE);
    }
}

/* Settings > Pictures and this checkbox are one setting; whichever moved
   it tells the other. */
static void pic_auto_sync(void)
{
    if (g_pic_auto)
        SendMessage(g_pic_auto, BM_SETCHECK, (WPARAM)(g_room_pics ? 1 : 0),
                    0L);
}

static void pic_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc;
    HPALETTE oldpal = NULL;
    unsigned char far *dib;

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);
    /* The control strip along the bottom, filled before anything else.
       This window has NO class background (pic_paint is supposed to cover
       every pixel) and it swallows WM_ERASEBKGND, while a checkbox paints
       only its own box and label - so the margins around it belonged to
       nobody, and kept whatever the screen happened to have there when a
       window was dragged across. Button-face grey, because that is the
       colour the checkbox's own label background comes back as. */
    {
        RECT sr = rc;

        sr.top = rc.bottom - (pic_list_h(hwnd) + PIC_AUTO_H);
        if (sr.top < 0)
            sr.top = 0;
        FillRect(hdc, &sr, GetStockObject(LTGRAY_BRUSH));
    }
    /* The checkbox strip and the browser list own the bottom bands;
       the art goes above them. */
    rc.bottom -= pic_list_h(hwnd) + PIC_AUTO_H;
    if (rc.bottom < 1)
        rc.bottom = 1;

    if (!g_pic_mem || !g_pic_w || !g_pic_h) {
        FillRect(hdc, &rc, GetStockObject(DKGRAY_BRUSH));
        SetBkMode(hdc, TRANSPARENT);
        SelectObject(hdc, GetStockObject(SYSTEM_FONT));
        SetTextColor(hdc, RGB(0xC0, 0xC0, 0xC0));
        DrawText(hdc, "No picture yet.", -1, &rc,
                 DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        EndPaint(hwnd, &ps);
        return;
    }

    if (g_pic_hpal) {
        oldpal = SelectPalette(hdc, g_pic_hpal, FALSE);
        RealizePalette(hdc);
    }
    dib = (unsigned char far *)GlobalLock(g_pic_mem);
    if (dib) {
        int dw, dh, dx;

        /* Aspect-fit to the window's width, top-aligned: the window is
           a column, and the space under the art belongs to the caption
           now and the browser list later. */
        dw = (int)rc.right;
        dh = (int)((long)dw * g_pic_h / g_pic_w);
        if (dh > (int)rc.bottom) {
            dh = (int)rc.bottom;
            dw = (int)((long)dh * g_pic_w / g_pic_h);
        }
        dx = ((int)rc.right - dw) / 2;
        FillRect(hdc, &rc, GetStockObject(BLACK_BRUSH));
        SetStretchBltMode(hdc, STRETCH_DELETESCANS);
        /* Bits start after the header and the always-256-entry colour
           table (the proxy writes biClrUsed=256, so 40+1024 is a
           constant, not a guess). */
        StretchDIBits(hdc, dx, 0, dw, dh, 0, 0, g_pic_w, g_pic_h,
                      (LPSTR)(dib + 40 + 1024), (LPBITMAPINFO)dib,
                      DIB_RGB_COLORS, SRCCOPY);
        GlobalUnlock(g_pic_mem);
        if (g_pic_title[0] && dh + g_ch < (int)rc.bottom) {
            RECT tr = rc;
            tr.top = dh + 4;
            SetBkMode(hdc, TRANSPARENT);
            SelectObject(hdc, GetStockObject(SYSTEM_FONT));
            SetTextColor(hdc, RGB(0xC0, 0xC0, 0xC0));
            DrawText(hdc, g_pic_title, -1, &tr,
                     DT_CENTER | DT_WORDBREAK | DT_NOPREFIX);
        }
    }
    if (oldpal)
        SelectPalette(hdc, oldpal, FALSE);
    EndPaint(hwnd, &ps);
}

long FAR PASCAL _export PicProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    LONG cres;
    int i;

    /* The 3.1 child caption and border. chrome_child_msg declares the
       non-client area in WM_NCCALCSIZE and paints it in WM_NCPAINT, so
       this window's CLIENT rect is unchanged and none of the layout
       below had to move. */
    if (chrome_child_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        /* Recorded here rather than from WM_MDICREATE's return: the
           first messages arrive before that call comes back. */
        g_pic_wnd = hwnd;
        /* The browser list. Repopulated from the shelf because this
           window can be closed and reopened mid-session - the shelf
           outlives it, like the transcript outlives its window. */
        g_pic_lb = CreateWindow("LISTBOX", NULL,
                                WS_CHILD | WS_BORDER | WS_VSCROLL
                                | LBS_NOTIFY,
                                0, 0, 10, 10, hwnd, (HMENU)ID_PICLIST,
                                LLM_INST(hwnd),
                                NULL);
        for (i = 0; i < g_shelf_count; i++)
            SendMessage(g_pic_lb, LB_ADDSTRING, 0,
                        (LONG)(LPSTR)(g_shelf[i].title[0]
                                      ? g_shelf[i].title
                                      : (char *)"(untitled)"));
        if (g_shelf_cur >= 0)
            SendMessage(g_pic_lb, LB_SETCURSEL, g_shelf_cur, 0L);
        /* "Illustrate every room" belongs where the pictures are, not
           three levels into a Settings dialog. Same variable, same INI
           key, same SET_OPTION - two ways to the one switch. */
        g_pic_auto = CreateWindow("BUTTON", "Illustrate every room",
                                  WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX,
                                  0, 0, 10, 10, hwnd, (HMENU)ID_PICAUTO,
                                  LLM_INST(hwnd),
                                  NULL);
        SendMessage(g_pic_auto, WM_SETFONT, (WPARAM)g_font, 0L);
        chrome_button(g_pic_auto);
        pic_auto_sync();
        break;

    case WM_PAINT:
        pic_paint(hwnd);
        return 0;

    case WM_ERASEBKGND:
        /* pic_paint covers every pixel; erasing first only flickers. */
        return 1;

    case WM_COMMAND:
        if (LLM_CMD_ID(wParam, lParam) == ID_PICLIST
            && LLM_CMD_NOTIFY(wParam, lParam) == LBN_SELCHANGE) {
            shelf_show((int)SendMessage(g_pic_lb, LB_GETCURSEL, 0, 0L));
            return 0;
        }
        if (LLM_CMD_ID(wParam, lParam) == ID_PICAUTO) {
            /* BS_AUTOCHECKBOX has already flipped its own state; read it
               rather than assuming, and persist immediately - a setting
               that needs OK is a setting people distrust. */
            g_room_pics = (int)SendMessage(g_pic_auto, BM_GETCHECK, 0, 0L)
                          ? 1 : 0;
            save_ini();
            if (net_state() == NET_UP) {
                send_options();
                set_status(g_room_pics
                           ? "Every location will be illustrated."
                           : "Only asked-for pictures now.");
            } else {
                set_status(g_room_pics
                           ? "Every location will be illustrated once "
                             "connected."
                           : "Only asked-for pictures now.");
            }
            if (g_input)
                SetFocus(g_input);
            return 0;
        }
        break;

    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        pic_layout(hwnd);
        InvalidateRect(hwnd, NULL, TRUE);
        break;

    case WM_DESTROY:
        desk_remember(DESK_PIC, hwnd);
        g_pic_wnd  = NULL;
        g_pic_lb   = NULL;
        g_pic_auto = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* menu_run lives with the menu dialog below; the music controls send
   commands through it. */
static void menu_run(HWND owner, int idx);

/* ---------------------------------------------------------------- */
/* The Music window                                                  */
/* ---------------------------------------------------------------- */

/* A command sent as if typed - what the Stop and Next buttons do, so
   the proxy stays the authority on what is playing (its MUSIC_STOP
   comes back and silences us; its next tune arrives as MIDI frames). */
static void send_command(const char *cmd)
{
    if (net_state() != NET_UP) {
        say(2, "Not connected. Use File > Connect.");
        return;
    }
    say(1, cmd);
    send_text_frame(MSG_CHAT_REQUEST, cmd);
    set_status("Waiting for the model...");
}

static void mus_layout(HWND hwnd)
{
    RECT rc;
    int i, bw, bh = g_ch + 10, x, py;

    GetClientRect(hwnd, &rc);
    bw = ((int)rc.right - 4 * 4) / 3;
    if (bw < 30) bw = 30;
    x = 4;
    for (i = 0; i < 3; i++) {
        if (g_mus_btn[i])
            MoveWindow(g_mus_btn[i], x, (int)rc.bottom - bh - 4,
                       bw, bh, TRUE);
        x += bw + 4;
    }
    /* The picker row sits above the transport row. The combo's height
       covers its dropped list, not the closed box - Win16 quirk. */
    py = (int)rc.bottom - bh * 2 - 8;
    if (g_mus_combo)
        MoveWindow(g_mus_combo, 4, py,
                   (int)rc.right - bw - 12, g_ch * 8, TRUE);
    if (g_mus_play)
        MoveWindow(g_mus_play, (int)rc.right - bw - 4, py, bw, bh, TRUE);
}

static void mus_fill_moods(void)
{
    int i;

    if (!g_mus_combo)
        return;
    SendMessage(g_mus_combo, CB_RESETCONTENT, 0, 0L);
    for (i = 0; i < g_mood_count; i++)
        SendMessage(g_mus_combo, CB_ADDSTRING, 0,
                    (LONG)(LPSTR)g_moods[i]);
    if (g_mood_count)
        SendMessage(g_mus_combo, CB_SETCURSEL, 0, 0L);
    if (g_mus_play)
        EnableWindow(g_mus_play, g_mood_count ? TRUE : FALSE);
}

static void mus_paint(HWND hwnd)
{
    static const char *state_name[] = { "Silent", "Playing", "Paused" };
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc;
    char line[96];
    int y = 6;

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);
    SetBkMode(hdc, TRANSPARENT);
    SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SetTextColor(hdc, RGB(0, 0, 0));
    if (g_mus_title[0]) {
        TextOut(hdc, 8, y, g_mus_title, lstrlen(g_mus_title));
        y += g_ch + 2;
        if (g_mus_author[0]) {
            wsprintf(line, "by %s", (LPSTR)g_mus_author);
            TextOut(hdc, 8, y, line, lstrlen(line));
            y += g_ch + 2;
        }
        if (g_mus_mood[0])
            wsprintf(line, "%s - mood: %s",
                     (LPSTR)state_name[g_mus_state], (LPSTR)g_mus_mood);
        else
            lstrcpy(line, state_name[g_mus_state]);
        TextOut(hdc, 8, y, line, lstrlen(line));
    } else {
        TextOut(hdc, 8, y, "Nothing has played yet.", 23);
        y += g_ch + 2;
        TextOut(hdc, 8, y, "The narrator starts the music,", 30);
        y += g_ch + 2;
        TextOut(hdc, 8, y, "or type /music <mood>.", 22);
    }
    EndPaint(hwnd, &ps);
}

long FAR PASCAL _export MusProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    LONG cres;
    static const char *label[3] = { "Pause", "Stop", "Next" };
    HINSTANCE inst;
    int i;

    /* The 3.1 child caption and border. chrome_child_msg declares the
       non-client area in WM_NCCALCSIZE and paints it in WM_NCPAINT, so
       this window's CLIENT rect is unchanged and none of the layout
       below had to move. */
    if (chrome_child_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        g_mus_wnd = hwnd;
        inst = LLM_INST(hwnd);
        for (i = 0; i < 3; i++)
            g_mus_btn[i] = CreateWindow("BUTTON", label[i],
                                        WS_CHILD | WS_VISIBLE
                                        | BS_PUSHBUTTON,
                                        0, 0, 10, 10, hwnd,
                                        (HMENU)(IDC_MUSBASE + i), inst,
                                        NULL);
        g_mus_combo = CreateWindow("COMBOBOX", NULL,
                                   WS_CHILD | WS_VISIBLE | WS_VSCROLL
                                   | CBS_DROPDOWNLIST,
                                   0, 0, 10, 10, hwnd,
                                   (HMENU)(IDC_MUSBASE + 3), inst, NULL);
        g_mus_play = CreateWindow("BUTTON", "Play",
                                  WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                                  0, 0, 10, 10, hwnd,
                                  (HMENU)(IDC_MUSBASE + 4), inst, NULL);
        chrome_buttons(hwnd);
        mus_fill_moods();
        mus_update();
        break;

    case WM_PAINT:
        mus_paint(hwnd);
        return 0;

    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        mus_layout(hwnd);
        InvalidateRect(hwnd, NULL, TRUE);
        break;

    case WM_COMMAND:
        switch (LLM_CMD_ID(wParam, lParam)) {
        case IDC_MUSBASE + 0:       /* Pause / Resume: purely local */
            if (g_mus_state == 1) {
                mciSendString("pause llm64mid", NULL, 0, NULL);
                g_mus_state = 2;
            } else if (g_mus_state == 2) {
                mciSendString("resume llm64mid", NULL, 0, NULL);
                g_mus_state = 1;
            }
            mus_update();
            return 0;
        case IDC_MUSBASE + 1:       /* Stop: the proxy's call */
            if (net_state() == NET_UP)
                send_command("/music stop");
            else
                mus_stop();
            return 0;
        case IDC_MUSBASE + 2:       /* Next: another of the same mood */
            send_command("/music next");
            return 0;
        case IDC_MUSBASE + 4: {     /* Play: the picked mood */
            char cmd[28];
            i = (int)SendMessage(g_mus_combo, CB_GETCURSEL, 0, 0L);
            if (i >= 0 && i < g_mood_count) {
                wsprintf(cmd, "/music %s", (LPSTR)g_moods[i]);
                send_command(cmd);
            }
            return 0;
        }
        }
        break;

    case WM_DESTROY:
        desk_remember(DESK_MUS, hwnd);
        g_mus_wnd = NULL;
        for (i = 0; i < 3; i++)
            g_mus_btn[i] = NULL;
        g_mus_combo = NULL;
        g_mus_play = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* ---------------------------------------------------------------- */
/* The Character and Inventory windows                               */
/* ---------------------------------------------------------------- */

/* Two views of the same STATE block: what a 1993 RPG put in its
   sidebars. Both float over the desk like the Music controls - they
   are gauges, not documents. */

static void chr_static_frame(const char *j)
{
    chr_static_parse(j);
    if (g_chr_wnd)
        InvalidateRect(g_chr_wnd, NULL, TRUE);
}

/* One word-wrapped line of the sheet, and the y it leaves behind. Returns
   zero when the window has run out of room, so the caller can stop
   drawing rather than scribble past the bottom edge - the sheet grew past
   what 230 pixels hold the day it learned race and class. */
static int chr_line(HDC hdc, const RECT *rc, int *y, const char *text)
{
    RECT tr;

    if (!text || !text[0])
        return 1;
    if (*y > (int)rc->bottom - g_ch)
        return 0;
    tr.left = 8;
    tr.top = *y;
    tr.right = (int)rc->right - 6;
    tr.bottom = (int)rc->bottom;
    DrawText(hdc, text, -1, &tr, DT_WORDBREAK | DT_NOPREFIX | DT_CALCRECT);
    if (tr.bottom > (int)rc->bottom)
        tr.bottom = (int)rc->bottom;
    tr.right = (int)rc->right - 6;
    DrawText(hdc, text, -1, &tr, DT_WORDBREAK | DT_NOPREFIX);
    *y = (int)tr.bottom + 2;
    return *y < (int)rc->bottom;
}

/* Every row of the sheet, present whether or not it is known: a blank
   where the AC should be is information ("the narrator is not tracking
   armour"), and a row that vanishes is just a sheet that looks different
   every turn. Unknowns read as a dash. */
#define CHR_UNKNOWN  "-"

static void chr_field(HDC hdc, const RECT *rc, int *y, int *out,
                      const char *label, const char *value)
{
    char line[260];

    if (!*out)
        return;
    wsprintf(line, "%s %s", (LPSTR)label,
             (LPSTR)(value && value[0] ? value : (char *)CHR_UNKNOWN));
    *out = chr_line(hdc, rc, y, line);
}

static void chr_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc, br;
    /* Sized for the longest thing that can land in it: a label plus a
       159-character companions list. It used to be 120, and three
       described companions wrote through the return address. */
    char line[260];
    char val[220];
    int y = 6, bh, ok = 1;

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);
    /* The button strip along the bottom belongs to the Refresh button,
       and this window has a grey class background, so the strip needs no
       filling - only keeping out of. */
    rc.bottom -= CHR_BTN_H;
    if (rc.bottom < g_ch)
        rc.bottom = g_ch;
    SetBkMode(hdc, TRANSPARENT);
    SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SetTextColor(hdc, RGB(0, 0, 0));

    /* Who this is. The proxy rolled it, the narrator never restates it,
       so it cannot drift mid-adventure. */
    {
        HFONT bold = g_fonts[SB_ATTR_BOLD];
        HFONT prev = bold ? SelectObject(hdc, bold) : NULL;
        const char *who = g_static.name[0] ? g_static.name
                                           : (char *)"(no character yet)";
        TextOut(hdc, 8, y, who, lstrlen(who));
        if (prev)
            SelectObject(hdc, prev);
        y += g_ch + 2;
    }
    val[0] = '\0';
    if (g_static.race[0])
        lstrcpy(val, g_static.race);
    if (g_static.cls[0]) {
        if (val[0])
            lstrcat(val, " ");
        lstrcat(val, g_static.cls);
    }
    if (g_sheet.has_level)
        wsprintf(val + lstrlen(val), "%sLevel %ld",
                 val[0] ? ", " : "", g_sheet.level);
    chr_field(hdc, &rc, &y, &ok, "", val);
    chr_field(hdc, &rc, &y, &ok, "", g_static.abil);
    chr_field(hdc, &rc, &y, &ok, "At:", g_sheet.location);
    if (!ok)
        goto done;

    /* HP keeps its bar - the one gauge worth drawing rather than
       spelling. An unknown HP still gets its row, empty. */
    bh = g_ch;
    if (g_sheet.has_hp)
        wsprintf(line, "HP %ld / %ld", g_sheet.hp, g_sheet.maxhp);
    else
        lstrcpy(line, "HP " CHR_UNKNOWN);
    TextOut(hdc, 8, y, line, lstrlen(line));
    if (g_sheet.has_hp) {
        br.left = 90; br.top = y + 1;
        br.right = (int)rc.right - 10; br.bottom = y + bh - 1;
        if (br.right > br.left + 10) {
            HBRUSH fill = CreateSolidBrush(
                (g_sheet.maxhp > 0 && g_sheet.hp * 4 <= g_sheet.maxhp)
                ? RGB(0xC0, 0x20, 0x20) : RGB(0x20, 0x80, 0x30));
            RECT in = br;
            FrameRect(hdc, &br, GetStockObject(BLACK_BRUSH));
            in.left++; in.top++; in.right--; in.bottom--;
            if (g_sheet.maxhp > 0) {
                long w = (long)(in.right - in.left) * g_sheet.hp
                    / g_sheet.maxhp;
                if (w < 0) w = 0;
                if (w > in.right - in.left) w = in.right - in.left;
                in.right = in.left + (int)w;
            }
            FillRect(hdc, &in, fill);
            DeleteObject(fill);
        }
    }
    y += bh + 4;

    if (g_sheet.has_mana)
        wsprintf(val, "%ld / %ld", g_sheet.mana, g_sheet.maxmana);
    else
        val[0] = '\0';
    chr_field(hdc, &rc, &y, &ok, "Mana", val);

    val[0] = '\0';
    if (g_sheet.has_ac)
        wsprintf(val, "AC %ld   ", g_sheet.ac);
    else
        lstrcpy(val, "AC " CHR_UNKNOWN "   ");
    if (g_sheet.has_gold)
        wsprintf(val + lstrlen(val), "Gold %ld   ", g_sheet.gold);
    else
        lstrcat(val, "Gold " CHR_UNKNOWN "   ");
    if (g_sheet.has_xp)
        wsprintf(val + lstrlen(val), "XP %ld   ", g_sheet.xp);
    else
        lstrcat(val, "XP " CHR_UNKNOWN "   ");
    if (g_sheet.has_score)
        wsprintf(val + lstrlen(val), "Score %ld", g_sheet.score);
    else
        lstrcat(val, "Score " CHR_UNKNOWN);
    if (!chr_line(hdc, &rc, &y, val))
        goto done;

    chr_field(hdc, &rc, &y, &ok, "Afflicted:", g_sheet.effects);
    chr_field(hdc, &rc, &y, &ok, "Skills:", g_static.skills);
    chr_field(hdc, &rc, &y, &ok, "Spells:", g_static.spells);
    chr_field(hdc, &rc, &y, &ok, "Kit:", g_static.gear);
    chr_field(hdc, &rc, &y, &ok, "Looks:", g_sheet.appearance);
    chr_field(hdc, &rc, &y, &ok, "With you:", g_sheet.companions);
    if (!ok)
        goto done;

    /* How old this is. The narrator drops the state block often enough
       that a sheet can be several turns stale while looking live, and the
       only cure is to say so - that is what Refresh is for. */
    if (g_sheet.has_age && g_sheet.age > 0) {
        SetTextColor(hdc, RGB(0x80, 0x00, 0x00));
        wsprintf(line, g_sheet.age == 1 ? "(as of %ld turn ago)"
                                        : "(as of %ld turns ago)",
                 g_sheet.age);
        chr_line(hdc, &rc, &y, line);
        SetTextColor(hdc, RGB(0, 0, 0));
    }
done:
    EndPaint(hwnd, &ps);
}

long FAR PASCAL _export ChrProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    LONG cres;
    RECT rc;

    /* The 3.1 child caption and border. chrome_child_msg declares the
       non-client area in WM_NCCALCSIZE and paints it in WM_NCPAINT, so
       this window's CLIENT rect is unchanged and none of the layout
       below had to move. */
    if (chrome_child_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        g_chr_wnd = hwnd;
        /* Refresh asks the narrator for a state block against the schema,
           rather than waiting for one to be volunteered. It costs a model
           call, so it is a button and not a timer. */
        g_chr_btn = CreateWindow("BUTTON", "Refresh",
                                 WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                                 0, 0, 10, 10, hwnd, (HMENU)ID_CHRREFRESH,
                                 LLM_INST(hwnd),
                                 NULL);
        SendMessage(g_chr_btn, WM_SETFONT, (WPARAM)g_font, 0L);
        chrome_button(g_chr_btn);
        break;
    case WM_PAINT:
        chr_paint(hwnd);
        return 0;
    case WM_COMMAND:
        if (LLM_CMD_ID(wParam, lParam) == ID_CHRREFRESH) {
            /* '/sheet' is a real command, so the C64 can type it too. The
               proxy answers with its stored halves immediately and asks
               the model for a fresh state block. */
            send_command("/sheet");
            if (g_input)
                SetFocus(g_input);
            return 0;
        }
        break;
    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        if (g_chr_btn) {
            GetClientRect(hwnd, &rc);
            MoveWindow(g_chr_btn, 6, (int)rc.bottom - CHR_BTN_H + 2,
                       72, CHR_BTN_H - 6, TRUE);
        }
        InvalidateRect(hwnd, NULL, TRUE);
        break;
    case WM_DESTROY:
        desk_remember(DESK_CHR, hwnd);
        g_chr_wnd = NULL;
        g_chr_btn = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

static void inv_fill(void)
{
    int i;

    if (!g_inv_lb)
        return;
    SendMessage(g_inv_lb, LB_RESETCONTENT, 0, 0L);
    for (i = 0; i < g_sheet.inv_n && i < 16; i++)
        SendMessage(g_inv_lb, LB_ADDSTRING, 0,
                    (LONG)(LPSTR)g_sheet.inv[i]);
    if (!g_sheet.inv_n)
        SendMessage(g_inv_lb, LB_ADDSTRING, 0,
                    (LONG)(LPSTR)"(empty-handed)");
    if (g_inv_wnd) {
        char title[48];
        /* Say when the list is longer than the window holds, rather than
           titling 20 items "(16)" and looking like the narrator lost
           four of them. */
        if (g_sheet.inv_total > g_sheet.inv_n)
            wsprintf(title, "Inventory (%d of %d)", g_sheet.inv_n,
                     g_sheet.inv_total);
        else if (g_sheet.inv_n)
            wsprintf(title, "Inventory (%d)", g_sheet.inv_n);
        else
            lstrcpy(title, "Inventory");
        SetWindowText(g_inv_wnd, title);
    }
}

long FAR PASCAL _export InvProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    LONG cres;
    RECT rc;

    /* The 3.1 child caption and border. chrome_child_msg declares the
       non-client area in WM_NCCALCSIZE and paints it in WM_NCPAINT, so
       this window's CLIENT rect is unchanged and none of the layout
       below had to move. */
    if (chrome_child_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        g_inv_wnd = hwnd;
        g_inv_lb = CreateWindow("LISTBOX", NULL,
                                WS_CHILD | WS_VISIBLE | WS_BORDER
                                | WS_VSCROLL,
                                0, 0, 10, 10, hwnd, (HMENU)ID_INVLIST,
                                LLM_INST(hwnd),
                                NULL);
        inv_fill();
        break;
    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        if (g_inv_lb) {
            GetClientRect(hwnd, &rc);
            MoveWindow(g_inv_lb, 0, 0, rc.right, rc.bottom, TRUE);
        }
        break;
    case WM_DESTROY:
        desk_remember(DESK_INV, hwnd);
        g_inv_wnd = NULL;
        g_inv_lb = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

static void sheet_update(void)
{
    if (g_chr_wnd)
        InvalidateRect(g_chr_wnd, NULL, TRUE);
    inv_fill();
}

static void sheet_open(const char *cls, HWND *slot, int which,
                       int x, int y, int cx, int cy)
{
    MDICREATESTRUCT mcs;

    if (*slot) {
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)*slot, 0L);
        return;
    }
    mcs.szClass = cls;
    mcs.szTitle = cls[5] == 'C' ? "Character" : "Inventory";
    mcs.hOwner  = LLM_INST(g_frame);
    desk_place(which, &mcs, x, y, cx, cy);
    mcs.style = 0;
    /* Opening a sheet is a good moment to ask for the proxy's stored copy:
       free, no LLM call, and it fills a window that would otherwise sit
       empty until the next turn. */
    if (net_state() == NET_UP)
        send_frame(MSG_GET_SHEET, NULL, 0);
    mcs.lParam = 0;
    g_in_layout = 1;
    SendMessage(g_mdi, WM_MDICREATE, 0, (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
}

/* The Notebook, opened if it is not already on the desk. */
static void note_open(void)
{
    MDICREATESTRUCT mcs;

    if (g_note_wnd) {
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_note_wnd, 0L);
        return;
    }
    mcs.szClass = NOTE_CLASS;
    mcs.szTitle = "Notebook";
    mcs.hOwner  = LLM_INST(g_frame);
    /* Wide enough for the index and a whole 78-column printed page, which
       is what the proxy laid the sheet out to: the page half does not
       re-flow (it is already typeset), so a narrower window clips it. */
    desk_place(DESK_NOTE, &mcs, 40, 24,
               NOTE_INDEX_W + g_cw * 78 + GetSystemMetrics(SM_CXVSCROLL) + 8,
               g_ch * 20);
    mcs.style   = 0;
    mcs.lParam  = 0;
    g_in_layout = 1;
    SendMessage(g_mdi, WM_MDICREATE, 0, (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
}

/* The Map window, opened if it is not already on the desk. Sized to the
   grid it has been told about rather than to a guess: an eight-room
   cellar and a forty-room castle want different windows. */
static void map_open(void)
{
    MDICREATESTRUCT mcs;
    int w, h;

    if (g_map_wnd) {
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_map_wnd, 0L);
        return;
    }
    w = 8 + (g_map_cols > 0 ? g_map_cols : 4) * 74;
    h = g_ch + 14 + (g_map_rows > 0 ? g_map_rows : 3) * 46;
    if (w < 240) w = 240;
    if (w > 560) w = 560;
    if (h < 180) h = 180;
    if (h > 420) h = 420;
    mcs.szClass = MAP_CLASS;
    mcs.szTitle = "Map";
    mcs.hOwner  = LLM_INST(g_frame);
    /* Same free refresh the sheet windows ask for. */
    if (net_state() == NET_UP)
        send_frame(MSG_GET_SHEET, NULL, 0);
    desk_place(DESK_MAP, &mcs, 56, 32, w, h);
    mcs.style   = 0;
    mcs.lParam  = 0;
    g_in_layout = 1;
    SendMessage(g_mdi, WM_MDICREATE, 0, (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
}

/* A finished sheet: file it, and put the Notebook where it can be seen.
   The window is opened rather than merely updated - a printout the player
   asked for is a reply, and a reply that lands in a closed drawer reads
   as nothing having happened. */
static void note_add(int slot)
{
    note_open();
    note_fill();
    note_show(slot);
    if (g_note_wnd)
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_note_wnd, 0L);
}

static void mus_open_wnd(void)
{
    MDICREATESTRUCT mcs;

    if (g_mus_wnd) {
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_mus_wnd, 0L);
        return;
    }
    mcs.szClass = MUS_CLASS;
    mcs.szTitle = "Music";
    mcs.hOwner  = LLM_INST(g_frame);
    desk_place(DESK_MUS, &mcs, 60, 40, 250, 140);
    mcs.style   = 0;
    mcs.lParam  = 0;
    /* Same guard as every open: birth is not the user arranging. */
    g_in_layout = 1;
    SendMessage(g_mdi, WM_MDICREATE, 0, (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
}

/* File > Save Picture As: the DIB with a BITMAPFILEHEADER in front of
   it IS a .BMP - the whole reason the wire format is what it is. */
static void do_save_pic(HWND hwnd)
{
    OPENFILENAME ofn;
    char file[144];
    BITMAPFILEHEADER bf;
    HFILE f;
    int failed = 0;

    if (!g_pic_mem) {
        MessageBox(hwnd, "No picture to save yet.", APP_TITLE,
                   MB_OK | MB_ICONINFORMATION);
        return;
    }
    lstrcpy(file, "PICTURE.BMP");
    memset(&ofn, 0, sizeof(ofn));
    ofn.lStructSize = sizeof(OPENFILENAME);
    ofn.hwndOwner   = hwnd;
    ofn.lpstrFilter = "Bitmap (*.BMP)\0*.bmp\0All files (*.*)\0*.*\0";
    ofn.nFilterIndex = 1;
    ofn.lpstrFile   = file;
    ofn.nMaxFile    = sizeof(file);
    ofn.lpstrDefExt = "bmp";
    ofn.Flags       = OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
                    | OFN_HIDEREADONLY;
    if (!GetSaveFileName(&ofn))
        return;

    bf.bfType      = 0x4D42;                    /* 'BM' */
    bf.bfSize      = sizeof(bf) + g_pic_size;
    bf.bfReserved1 = 0;
    bf.bfReserved2 = 0;
    bf.bfOffBits   = sizeof(bf) + 40 + 1024;

    f = _lcreat(file, 0);
    if (f == HFILE_ERROR) {
        MessageBox(hwnd, "Couldn't create that file.", APP_TITLE,
                   MB_OK | MB_ICONEXCLAMATION);
        return;
    }
    if (_lwrite(f, (LPSTR)&bf, sizeof(bf)) != sizeof(bf))
        failed = 1;
    if (!failed && !hfile_write(f, g_pic_mem, g_pic_size))
        failed = 1;
    _lclose(f);
    if (failed) {
        MessageBox(hwnd, "The save didn't finish - disk full?",
                   APP_TITLE, MB_OK | MB_ICONEXCLAMATION);
    } else {
        char msg[176];
        wsprintf(msg, "Saved picture to %s.", (LPSTR)file);
        set_status(msg);
    }
}

static void paint_status(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc, sr;
    int statush = g_ch + 6;

    hdc = BeginPaint(hwnd, &ps);
    /* The frame, caption and menu bar first; then our own furniture
       inside what is left. */
    chrome_paint(hwnd, hdc);
    frame_content(hwnd, &rc);
    /* The class background no longer runs - the chrome claims
       WM_ERASEBKGND, because it covers every pixel it owns - so the strip
       behind the launcher buttons is ours to fill. WS_CLIPCHILDREN keeps
       this off the MDI client. */
    FillRect(hdc, &rc, GetStockObject(LTGRAY_BRUSH));
    sr = rc;
    sr.top = rc.bottom - statush;
    FillRect(hdc, &sr, GetStockObject(LTGRAY_BRUSH));
    /* The sunken top edge every 3.1 status strip had */
    MoveTo(hdc, sr.left, sr.top);
    LineTo(hdc, sr.right, sr.top);
    /* And the same edge under the launcher strip, so it reads as a
       toolbar rather than as buttons loose on the background. */
    MoveTo(hdc, rc.left, rc.top + launch_h() - 1);
    LineTo(hdc, rc.right, rc.top + launch_h() - 1);
    SetBkMode(hdc, TRANSPARENT);
    SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SetTextColor(hdc, RGB(0, 0, 0));
    TextOut(hdc, (int)rc.left + 4, sr.top + 3, g_status, lstrlen(g_status));
    if (g_chrome[0]) {
        int n = lstrlen(g_chrome);
        int w = LOWORD(GetTextExtent(hdc, g_chrome, n));
        TextOut(hdc, (int)sr.right - w - 6, sr.top + 3, g_chrome, n);
    }
    EndPaint(hwnd, &ps);
}

static int g_started = 0;

/* Banner, Winsock, first connect - everything that wants a laid-out
   window. Called once, from the first WM_SIZE. */
static void start_session(HWND hwnd)
{
    char err[128];

    /* The banner is also the renderer's self-check: it is written in
       the same in-band marker language the proxy streams, so if colour
       is broken it is broken before the first frame arrives. */
    say(1, "LLM64 for Windows - Phase 1. "
           "The transcript keeps its lines unwrapped and re-flows them "
           "at paint time, so resizing this window re-lays out the text "
           "already in it rather than leaving it wrapped where it fell.");
    say(12, "In-band markers: "
            "\x12" "red" "\x01" " "
            "\x1D" "green" "\x01" " "
            "\x17" "yellow" "\x01" " "
            "\x02" "bold" "\x03" ".");

    if (!net_init(hwnd, err, sizeof(err))) {
        set_status(err);
        say(2, err);
        return;
    }
    do_connect();
}

static void save_ini(void);

/* The menu, as a 1993 program would have shown it: a modal box of
   pushbuttons. The buttons are made here rather than in the resource
   because the proxy decides how many there are and what they say, and
   each carries the proxy's own key as its mnemonic - so the muscle
   memory from the C64's F1 panel still works. */
BOOL FAR PASCAL _export MenuDlgProc(HWND dlg, UINT msg, UINT wParam,
                                    LONG lParam)
{
    LONG cres;
    int i, rows, cols, x, y, w, h;
    /* mgy clears the caption line above the buttons - the static text is
       at 6 dialog units, which is lower than it looks. */
    int bw = 190, bh = 26, gap = 6, mgx = 12, mgy = 34;
    HINSTANCE inst;
    RECT wr, cr, fr;
    char text[52];

    (void)lParam;
    /* The 3.1 palette for the controls. Ahead of chrome_dialog_msg and
       returned directly, because the answer is a brush and DWL_MSGRESULT
       is not where the dialog manager looks for one - see chrome.h. */
    if (LLM_IS_CTLCOLOR(msg) && chrome_ctlcolor(msg, wParam, lParam, &cres))
        return (BOOL)cres;
    /* The 3.1 dialog frame. A DialogProc cannot return an arbitrary
       value, so the result goes back through DWL_MSGRESULT. */
    if (chrome_dialog_msg(dlg, msg, wParam, lParam, &cres)) {
        SetWindowLong(dlg, DWL_MSGRESULT, cres);
        return TRUE;
    }

    switch (msg) {
    case WM_INITDIALOG:
        inst = LLM_INST(dlg);
        SetDlgItemText(dlg, IDC_MENUTITLE, g_menu_count
            ? "Choose an action:"
            : "The proxy has not sent its menu yet. Connect, then try F1.");

        cols = (g_menu_count > 7) ? 2 : 1;
        rows = cols ? (g_menu_count + cols - 1) / cols : 1;
        if (rows < 1) rows = 1;
        for (i = 0; i < g_menu_count; i++) {
            /* "Cancel reply (&x)" - the label the proxy wrote, and the
               key it chose, in the place Windows expects a mnemonic. */
            wsprintf(text, "%s (&%c)", (LPSTR)g_menu[i].label,
                     g_menu[i].key);
            CreateWindow("BUTTON", text,
                         WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
                         mgx + (i / rows) * (bw + gap),
                         mgy + (i % rows) * (bh + gap),
                         bw, bh, dlg, (HMENU)(IDC_MENUBASE + i), inst, NULL);
        }

        /* Fit the box to what it actually holds - a menu of four should
           not open a window sized for thirteen. */
        w = mgx * 2 + cols * bw + (cols - 1) * gap;
        h = mgy + rows * (bh + gap) + 10 + bh;
        GetWindowRect(dlg, &wr);
        GetClientRect(dlg, &cr);
        w += (int)(wr.right - wr.left) - (int)cr.right;
        h += (int)(wr.bottom - wr.top) - (int)cr.bottom;
        /* Centred on the frame, which is where a modal belongs. */
        GetWindowRect(g_frame, &fr);
        x = (int)fr.left + ((int)(fr.right - fr.left) - w) / 2;
        y = (int)fr.top + ((int)(fr.bottom - fr.top) - h) / 2;
        if (x < 0) x = 0;
        if (y < 0) y = 0;
        SetWindowPos(dlg, NULL, x, y, w, h, SWP_NOZORDER);
        MoveWindow(GetDlgItem(dlg, IDCANCEL),
                   (w - 70) / 2, mgy + rows * (bh + gap) + 4, 70, bh, TRUE);
        /* Again, and by hand: the chrome skins a dialog's buttons when
           WM_INITDIALOG reaches it, and this one's buttons did not exist
           yet - they are built from the proxy's menu a few lines up. */
        chrome_buttons(dlg);
        return TRUE;

    case WM_COMMAND:
        if (LLM_CMD_ID(wParam, lParam) >= IDC_MENUBASE
            && LLM_CMD_ID(wParam, lParam) < IDC_MENUBASE + MAX_MENU) {
            g_menu_choice = (int)(LLM_CMD_ID(wParam, lParam) - IDC_MENUBASE);
            EndDialog(dlg, 1);
            return TRUE;
        }
        if (LLM_CMD_ID(wParam, lParam) == IDCANCEL) {
            EndDialog(dlg, 0);
            return TRUE;
        }
        break;
    }
    return FALSE;
}

/* Server settings. The one dialog the client cannot do without on a real
   machine: there is no command line there, so without this the only way
   to change the address is to rebuild the disk the program came on. */
BOOL FAR PASCAL _export PicsDlgProc(HWND dlg, UINT msg, UINT wParam,
                                    LONG lParam)
{
    LONG cres;
    (void)lParam;
    /* The 3.1 palette for the controls. Ahead of chrome_dialog_msg and
       returned directly, because the answer is a brush and DWL_MSGRESULT
       is not where the dialog manager looks for one - see chrome.h. */
    if (LLM_IS_CTLCOLOR(msg) && chrome_ctlcolor(msg, wParam, lParam, &cres))
        return (BOOL)cres;
    /* The 3.1 dialog frame. A DialogProc cannot return an arbitrary
       value, so the result goes back through DWL_MSGRESULT. */
    if (chrome_dialog_msg(dlg, msg, wParam, lParam, &cres)) {
        SetWindowLong(dlg, DWL_MSGRESULT, cres);
        return TRUE;
    }

    switch (msg) {
    case WM_INITDIALOG:
        CheckDlgButton(dlg, IDC_ROOMPICS, g_room_pics);
        return TRUE;

    case WM_COMMAND:
        switch (LLM_CMD_ID(wParam, lParam)) {
        case IDOK:
            g_room_pics = IsDlgButtonChecked(dlg, IDC_ROOMPICS) ? 1 : 0;
            save_ini();
            EndDialog(dlg, 1);
            return TRUE;

        case IDCANCEL:
            EndDialog(dlg, 0);
            return TRUE;
        }
        break;
    }
    return FALSE;
}

/* Settings > Pictures. The new answer goes to the proxy immediately if
   the link is up; a later connect re-sends it either way. */
static void pics_dialog(HWND owner)
{
    HINSTANCE inst = LLM_INST(owner);
    FARPROC fn = MakeProcInstance((FARPROC)PicsDlgProc, inst);
    int r = DialogBox(inst, "LLM64PICS", owner, (DLGPROC)fn);

    FreeProcInstance(fn);
    if (r != 1)
        return;
    /* The picture window shows the same switch; it must not go on saying
       the opposite. */
    pic_auto_sync();
    if (net_state() == NET_UP) {
        send_options();
        set_status(g_room_pics
                   ? "Every location will be illustrated."
                   : "Only asked-for pictures now.");
    }
}

/* ---------------------------------------------------------------- */
/* Conversations: the browser's dialog side                          */
/* ---------------------------------------------------------------- */

static void conv_fill(HWND dlg)
{
    HWND lb = GetDlgItem(dlg, IDC_CONVLIST);
    int i;

    SendMessage(lb, LB_RESETCONTENT, 0, 0L);
    if (g_conv_waiting) {
        SendMessage(lb, LB_ADDSTRING, 0, (LONG)(LPSTR)"(loading...)");
        return;
    }
    if (!g_conv_count) {
        SendMessage(lb, LB_ADDSTRING, 0,
                    (LONG)(LPSTR)"(no conversations yet)");
        return;
    }
    for (i = 0; i < g_conv_count; i++)
        SendMessage(lb, LB_ADDSTRING, 0, (LONG)(LPSTR)g_convs[i].title);
    SendMessage(lb, LB_SETCURSEL, 0, 0L);
    /* 'More' pages forward, wrapping home from the last page - the
       C64's browser walks the same way. */
    EnableWindow(GetDlgItem(dlg, IDC_CONVMORE),
                 (g_conv_more_pages || g_conv_page > 0) ? TRUE : FALSE);
}

/* The selected row's entry, or -1. The placeholder rows above make a
   selection index only trustworthy when real entries are listed. */
static int conv_sel(HWND dlg)
{
    int i;

    if (g_conv_waiting || !g_conv_count)
        return -1;
    i = (int)SendMessage(GetDlgItem(dlg, IDC_CONVLIST),
                         LB_GETCURSEL, 0, 0L);
    return (i >= 0 && i < g_conv_count) ? i : -1;
}

static void conv_send_id(unsigned char type, unsigned long id)
{
    unsigned char p[4];

    p[0] = (unsigned char)(id & 0xFF);
    p[1] = (unsigned char)((id >> 8) & 0xFF);
    p[2] = (unsigned char)((id >> 16) & 0xFF);
    p[3] = (unsigned char)((id >> 24) & 0xFF);
    send_frame(type, p, 4);
}

BOOL FAR PASCAL _export ConvDlgProc(HWND dlg, UINT msg, UINT wParam,
                                    LONG lParam)
{
    LONG cres;
    int i;
    char q[96];
    UINT cmd;

    /* The 3.1 palette for the controls. Ahead of chrome_dialog_msg and
       returned directly, because the answer is a brush and DWL_MSGRESULT
       is not where the dialog manager looks for one - see chrome.h. */
    if (LLM_IS_CTLCOLOR(msg) && chrome_ctlcolor(msg, wParam, lParam, &cres))
        return (BOOL)cres;
    /* The 3.1 dialog frame. A DialogProc cannot return an arbitrary
       value, so the result goes back through DWL_MSGRESULT. */
    if (chrome_dialog_msg(dlg, msg, wParam, lParam, &cres)) {
        SetWindowLong(dlg, DWL_MSGRESULT, cres);
        return TRUE;
    }

    switch (msg) {
    case WM_INITDIALOG:
        g_convdlg = dlg;
        conv_fill(dlg);
        return TRUE;

    case WM_CONVS_READY:
        conv_fill(dlg);
        return TRUE;

    case WM_COMMAND:
        /* Double-clicking a row is Load - every 1993 file box agrees. */
        cmd = LLM_CMD_ID(wParam, lParam);
        if (cmd == IDC_CONVLIST
            && LLM_CMD_NOTIFY(wParam, lParam) == LBN_DBLCLK)
            cmd = IDC_CONVLOAD;
        switch (cmd) {
        case IDC_CONVLOAD:
            i = conv_sel(dlg);
            if (i < 0)
                return TRUE;
            conv_send_id(MSG_LOAD_CONVERSATION, g_convs[i].id);
            g_ack_quiet++;          /* the load leads with a bare ACK */
            set_status("Loading conversation...");
            EndDialog(dlg, 1);
            return TRUE;

        case IDC_CONVNEW:
            EndDialog(dlg, 2);      /* the caller owns new_conversation */
            return TRUE;

        case IDC_CONVSTAR:
            i = conv_sel(dlg);
            if (i < 0)
                return TRUE;
            conv_send_id(MSG_STAR_CONVERSATION, g_convs[i].id);
            g_ack_quiet++;
            /* Re-list so the '*' prefix (the proxy renders it into the
               title) appears without inventing client-side state. */
            conv_request_page(g_conv_page);
            conv_fill(dlg);
            return TRUE;

        case IDC_CONVDEL:
            i = conv_sel(dlg);
            if (i < 0)
                return TRUE;
            wsprintf(q, "Delete \"%s\"?\n\nThis cannot be undone.",
                     (LPSTR)g_convs[i].title);
            if (MessageBox(dlg, q, "Delete Conversation",
                           MB_YESNO | MB_ICONQUESTION) != IDYES)
                return TRUE;
            conv_send_id(MSG_DELETE_CONVERSATION, g_convs[i].id);
            g_ack_quiet++;
            conv_request_page(g_conv_page);
            conv_fill(dlg);
            return TRUE;

        case IDC_CONVMORE:
            g_conv_page = g_conv_more_pages ? g_conv_page + 1 : 0;
            conv_request_page(g_conv_page);
            conv_fill(dlg);
            return TRUE;

        case IDCANCEL:
            EndDialog(dlg, 0);
            return TRUE;
        }
        break;

    case WM_DESTROY:
        g_convdlg = NULL;
        break;
    }
    return FALSE;
}

/* Put the browser up. The list request goes out BEFORE the dialog so
   the round trip overlaps the window appearing; the frames land in
   conv_list_frame because the modal loop still dispatches the frame
   window's socket messages. */
static int conv_dialog(HWND owner)
{
    HINSTANCE inst = LLM_INST(owner);
    FARPROC fn;
    int r;

    if (net_state() != NET_UP) {
        say(2, "Not connected. Use File > Connect.");
        return 0;
    }
    g_conv_page = 0;
    conv_request_page(0);
    fn = MakeProcInstance((FARPROC)ConvDlgProc, inst);
    r = DialogBox(inst, "LLM64CONVS", owner, (DLGPROC)fn);
    FreeProcInstance(fn);
    return r;
}

BOOL FAR PASCAL _export ServerDlgProc(HWND dlg, UINT msg, UINT wParam,
                                      LONG lParam)
{
    LONG cres;
    char buf[64];
    unsigned port;

    (void)lParam;
    /* The 3.1 palette for the controls. Ahead of chrome_dialog_msg and
       returned directly, because the answer is a brush and DWL_MSGRESULT
       is not where the dialog manager looks for one - see chrome.h. */
    if (LLM_IS_CTLCOLOR(msg) && chrome_ctlcolor(msg, wParam, lParam, &cres))
        return (BOOL)cres;
    /* The 3.1 dialog frame. A DialogProc cannot return an arbitrary
       value, so the result goes back through DWL_MSGRESULT. */
    if (chrome_dialog_msg(dlg, msg, wParam, lParam, &cres)) {
        SetWindowLong(dlg, DWL_MSGRESULT, cres);
        return TRUE;
    }

    switch (msg) {
    case WM_INITDIALOG:
        SetDlgItemText(dlg, IDC_HOST, g_host);
        SetDlgItemInt(dlg, IDC_PORT, g_port, FALSE);
        CheckDlgButton(dlg, IDC_RECONNECT, 1);
        return TRUE;

    case WM_COMMAND:
        switch (LLM_CMD_ID(wParam, lParam)) {
        case IDOK:
            GetDlgItemText(dlg, IDC_HOST, buf, sizeof(buf) - 1);
            port = GetDlgItemInt(dlg, IDC_PORT, NULL, FALSE);
            if (!buf[0] || port == 0) {
                MessageBox(dlg, "A host and a port are both needed.",
                           APP_TITLE, MB_OK | MB_ICONEXCLAMATION);
                return TRUE;
            }
            lstrcpyn(g_host, buf, sizeof(g_host) - 1);
            g_port = port;
            save_ini();
            /* Reconnecting is the caller's job, not this proc's: doing
               it here would re-enter the socket code from inside a
               modal dialog that is already half torn down. The result
               code carries the intent out instead. */
            EndDialog(dlg, IsDlgButtonChecked(dlg, IDC_RECONNECT) ? 2 : 1);
            return TRUE;

        case IDCANCEL:
            EndDialog(dlg, 0);
            return TRUE;
        }
        break;
    }
    return FALSE;
}

/* Put the Server box up and act on what it decided. Reached from the
   Settings menu and from the proxy's own "Server config" entry. */
static void server_dialog(HWND owner)
{
    HINSTANCE inst = LLM_INST(owner);
    /* MakeProcInstance is not optional in Win16: the dialog is called
       back through a thunk that reloads DS for this instance. */
    FARPROC fn = MakeProcInstance((FARPROC)ServerDlgProc, inst);
    int r = DialogBox(inst, "LLM64SERVER", owner, (DLGPROC)fn);

    FreeProcInstance(fn);
    if (r == 2) {
        net_disconnect();
        do_connect();
    } else if (r == 1) {
        char msg[160];
        wsprintf(msg, "Server set to %s:%u - connect when ready.",
                 (LPSTR)g_host, g_port);
        set_status(msg);
    }
}

static void new_conversation(void)
{
    send_frame(MSG_NEW_CONVERSATION, NULL, 0);
    sb_clear(&g_conv_view.sb);
    view_touch(&g_conv_view);
}

/* Act on a menu entry. '!' means the action lives here; anything else is
   a command the proxy understands, sent exactly as if it had been typed
   - which is why a new server-side command needs no client change. */
static void menu_run(HWND owner, int idx)
{
    const char *cmd;

    if (idx < 0 || idx >= g_menu_count)
        return;
    cmd = g_menu[idx].cmd;

    if (cmd[0] != '!') {
        if (net_state() != NET_UP) {
            say(2, "Not connected. Use File > Connect.");
            return;
        }
        say(1, cmd);
        send_text_frame(MSG_CHAT_REQUEST, cmd);
        set_status("Waiting for the model...");
        if (g_input)
            EnableWindow(g_input, FALSE);
        return;
    }

    switch (cmd[1]) {
    case 'n':
        new_conversation();
        break;
    case 'x':
        send_frame(MSG_CANCEL_REQUEST, NULL, 0);
        input_enable(1);
        set_status("Cancelled.");
        break;
    case 'e':
        server_dialog(owner);
        break;
    case 'd':
        /* mod_diskcopy exists because a C64 program has to be able to
           copy itself onto a blank disk. Nothing here does. */
        say(12, "Copying the client disk is a C64 problem.");
        break;
    case 'c':
        if (conv_dialog(owner) == 2)
            new_conversation();
        break;
    case 'j':
        say(12, "The jukebox is not built yet - music is MIDI here, "
                "and that is still to come.");
        break;
    default:
        say(12, "That entry has no Windows equivalent yet.");
        break;
    }
}

/* Open the conversation document. One today; the same call is how a
   picture viewer or a jukebox will arrive. */
static HWND conv_create(HWND frame)
{
    MDICREATESTRUCT mcs;

    HWND w;

    mcs.szClass = CONV_CLASS;
    mcs.szTitle = "Conversation";
    mcs.hOwner  = LLM_INST(frame);
    /* A real restored size, not CW_USEDEFAULT, and created *unmaximized*
       even though it is wanted maximized. Creating it maximized with
       CW_USEDEFAULT leaves the normal rect degenerate, so the first
       un-maximize restores the window to no area at all - squashed flat
       on Windows, and gone entirely under Wine. Maximizing afterwards
       records this rect as the one to come back to. */
    desk_place(DESK_CONV, &mcs, 8, 8,
               g_cw * 80 + 6 * GetSystemMetrics(SM_CXVSCROLL),
               g_ch * 24);
    mcs.style   = 0;
    mcs.lParam  = 0;

    /* Same guard as pic_open: creation-time WM_SIZEs are not the user
       arranging the desk. */
    g_in_layout = 1;
    w = LLM_HWND(SendMessage(g_mdi, WM_MDICREATE, 0,
                             (LONG)(LPMDICREATESTRUCT)&mcs));
    g_in_layout = 0;
    /* Not maximized any more: the desk holds two documents now, and
       layout_default puts this one beside the picture. Maximizing is
       one double-click away for anyone who wants the old look. */
    return w;
}

/* ---------------------------------------------------------------- */
/* The launcher's latched buttons                                    */
/* ---------------------------------------------------------------- */

/* Which of the launcher's windows are open, as a bitmask indexed by
   button. Lives here rather than up with the strip because it has to see
   every window handle in the file. */
static unsigned launch_state(void)
{
    return (g_conv     ? 0x0002u : 0)
         | (g_pic_wnd  ? 0x0004u : 0)
         | (g_mus_wnd  ? 0x0008u : 0)
         | (g_chr_wnd  ? 0x0010u : 0)
         | (g_inv_wnd  ? 0x0020u : 0)
         | (g_note_wnd ? 0x0040u : 0)
         | (g_map_wnd  ? 0x0080u : 0);
}

static unsigned g_launch_shown = 0xFFFFu;   /* forces the first paint */

/* Repaint the buttons when what they report has changed. Called on every
   toggle for an immediate answer, and off a timer as the backstop: a
   window can also be closed from its own system menu, with Ctrl+F4, or
   from the Window menu, and a button that lies about that is worse than
   no button at all. */
static void launch_sync(void)
{
    unsigned now = launch_state();
    int i;

    if (now == g_launch_shown)
        return;
    g_launch_shown = now;
    for (i = 1; i < LAUNCH_N; i++)
        if (g_launch[i])
            InvalidateRect(g_launch[i], NULL, TRUE);
}

#define ID_LAUNCHTIMER  1
#define LAUNCH_TICK_MS  400

/* The 50% stipple a 3.1 toolbar used for a latched button - Word 2 and
   Excel 4 both did this, and it is what makes "held down" read as a state
   rather than as a mouse still being held. A monochrome pattern brush
   takes its two colours from the DC's text and background, so the same
   brush works in any colour scheme. Built once. */
static HBRUSH g_stipple;

static HBRUSH stipple_brush(void)
{
    static const unsigned short bits[8] = {
        0x5555, 0xAAAA, 0x5555, 0xAAAA, 0x5555, 0xAAAA, 0x5555, 0xAAAA
    };
    HBITMAP bmp;

    if (g_stipple)
        return g_stipple;
    bmp = CreateBitmap(8, 8, 1, 1, (LPSTR)bits);
    if (!bmp)
        return NULL;
    g_stipple = CreatePatternBrush(bmp);
    DeleteObject(bmp);          /* the brush keeps its own copy */
    return g_stipple;
}

/* A button in the Windows 3.1 style, drawn by hand because 3.1 has no
   push-like checkbox (BS_PUSHLIKE is Win32) and no DrawFrameControl.
   The face itself is the chrome's, so this strip and every real BUTTON
   in the program are the same measured drawing - which they were not
   before: this one had a one pixel highlight where a real 3.1 button has
   two, and square corners where a real one has none.

   The 3.1 palette rather than GetSysColor, for the same reason the
   chrome hardcodes it: COLOR_BTNFACE on a modern machine is #F0F0F0,
   which put a 2026 grey button on a #C0C0C0 strip. The cost is that a
   3.11 user's colour scheme no longer reaches the strip - and it never
   reached the caption either, so the strip was the odd one out. */
static void launch_draw(LPDRAWITEMSTRUCT di)
{
    int idx = (int)di->CtlID - IDC_LAUNCHBASE;
    /* Slot 0 is the Menu button: an action, not a window, so it is never
       latched - only pushed while the mouse holds it. */
    int on = idx > 0 && (launch_state() & (1u << idx)) != 0;
    int down = on || (di->itemState & ODS_SELECTED) != 0;
    RECT r = di->rcItem;
    int n;
    char text[28];

    chrome_button_face(di->hDC, &r, down, 0);
    if (on && !(di->itemState & ODS_SELECTED)) {
        HBRUSH st = stipple_brush();

        if (st) {
            RECT fr = r;

            /* The face carries the toolbar stipple when the button is
               latched rather than merely held. Inset past the bevel on
               every side: the bevel is the signal, the stipple only says
               "and it stayed that way". Face against highlight rather
               than shadow against highlight - at 1:1 that is a faint
               dither, where grey-on-white was a checkerboard loud enough
               to bury the bevel it sits inside. */
            fr.left += 4;
            fr.top += 4;
            fr.right -= 3;
            fr.bottom -= 3;
            SetTextColor(di->hDC, RGB(0xC0, 0xC0, 0xC0));
            SetBkColor(di->hDC, RGB(0xFF, 0xFF, 0xFF));
            FillRect(di->hDC, &fr, st);
        }
    }

    n = GetWindowText(di->hwndItem, text, sizeof(text) - 1);
    text[n < 0 ? 0 : n] = '\0';
    SetBkMode(di->hDC, TRANSPARENT);
    SelectObject(di->hDC, g_font ? g_font : GetStockObject(SYSTEM_FONT));
    SetTextColor(di->hDC, RGB(0x00, 0x00, 0x00));
    if (down) {                 /* the label rides the bevel down */
        r.left += 1;
        r.top += 1;
    }
    DrawText(di->hDC, text, -1, &r,
             DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    if (di->itemState & ODS_FOCUS) {
        RECT fr = di->rcItem;
        InflateRect(&fr, -4, -4);
        DrawFocusRect(di->hDC, &fr);
    }
}

long FAR PASCAL _export FrameProc(HWND hwnd, UINT msg, UINT wParam,
                                  LONG lParam)
{
    char err[128];
    TEXTMETRIC tm;
    HDC hdc;
    CLIENTCREATESTRUCT ccs;
    LONG cres;

    /* The 3.1 frame. One line, before anything else: it owns the caption,
       the menu bar, the sizing border and the messages that go with them,
       and returns 0 for everything else so the application still sees it.
       WM_DRAWITEM is only claimed for ODT_MENU, which is what leaves the
       launcher strip's owner-drawn buttons alone. */
    if (chrome_msg(hwnd, msg, wParam, lParam, &cres))
        return cres;

    switch (msg) {
    case WM_CREATE:
        g_frame = hwnd;
        /* Colours before anything can paint: the background brush lives
           with the theme, and WM_CTLCOLOR hands it out. */
        theme_apply(g_theme);
        if (!sb_init(&g_conv_view.sb)) {
            MessageBox(hwnd, "Not enough memory for the transcript.",
                       APP_TITLE, MB_OK | MB_ICONSTOP);
            return -1;
        }
        g_conv_view.live   = 1;
        g_conv_view.wrap   = 1;     /* the transcript re-flows; paper does not */
        g_conv_view.margin = 2;
        g_conv_view.follow = 1;
        g_font = GetStockObject(SYSTEM_FIXED_FONT);
        hdc = GetDC(hwnd);
        SelectObject(hdc, g_font);
        GetTextMetrics(hdc, &tm);
        g_cw = tm.tmAveCharWidth;
        g_ch = tm.tmHeight;
        fonts_init(hdc);
        ReleaseDC(hwnd, hdc);

        /* The chrome draws the menu bar, so the resource menu is read for
           its popups and then DETACHED - leave it attached and Windows
           draws a second, 2026-styled bar above ours. SetMenu(NULL) only
           detaches; the HMENU stays valid, which matters because MDI is
           about to append the window list to one of its submenus and the
           chrome is holding that same handle. */
        g_menubar = GetMenu(hwnd);
        chrome_init(hwnd, g_menubar);
        SetMenu(hwnd, NULL);

        /* The MDI client owns the documents. It wants the Window menu
           by handle so it can append the child list to it, and the id
           it should start numbering those entries from. */
        ccs.hWindowMenu  = GetSubMenu(g_menubar, WINDOW_MENU_POS);
        ccs.idFirstChild = IDM_FIRSTCHILD;
        g_mdi = CreateWindow("MDICLIENT", NULL,
                             WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
                             0, 0, 10, 10, hwnd, (HMENU)1,
                             LLM_INST(hwnd),
                             (LPSTR)&ccs);
        if (!g_mdi)
            return -1;
        chrome_set_mdi(g_mdi);
        launch_create(hwnd);
        /* The buttons report what is open, and a window can close without
           going through them - the system menu, Ctrl+F4, the Window menu.
           A slow tick is the honest way to keep them true. */
        SetTimer(hwnd, ID_LAUNCHTIMER, LAUNCH_TICK_MS, NULL);
        g_conv = conv_create(hwnd);
        /* The picture window is part of the default desk, empty or not:
           an adventure fills it, and until then it says what it is.
           So are the Music controls, tucked in their corner - and the
           conversation takes the keyboard back from whatever opened
           last. */
        pic_open();
        mus_open_wnd();
        if (g_conv)
            SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_conv, 0L);
        return 0;

    case WM_SIZE:
        frame_layout(hwnd);
        /* Keep the two-document desk proportioned to the frame - but
           only while it is still ours. The first drag makes it the
           user's, and Window > Default Layout is the way back. */
        if (!g_user_arranged)
            layout_default();
        /* Text written before the pane knew its size used to wrap at the
           10x10 placeholder width and stay that way; now it re-flows on
           the first WM_SIZE like everything else. The session still
           starts from here rather than WM_CREATE, because connecting
           wants a window that is already on screen to report to. */
        if (!g_started) {
            g_started = 1;
            start_session(hwnd);
        }
        return 0;

    case WM_SETFOCUS:
        if (g_input)
            SetFocus(g_input);
        return 0;

    case WM_PAINT:
        paint_status(hwnd);
        return 0;

    case WM_DRAWITEM:
        if (wParam >= IDC_LAUNCHBASE
                && wParam < IDC_LAUNCHBASE + LAUNCH_N) {
            launch_draw((LPDRAWITEMSTRUCT)lParam);
            return TRUE;
        }
        break;

    case WM_TIMER:
        if (wParam == ID_LAUNCHTIMER) {
            launch_sync();
            return 0;
        }
        break;

    case NET_WM_SOCKET: {
        unsigned event = net_on_socket_msg(wParam, lParam, err, sizeof(err));
        if (event == NET_EV_CONNECT) {
            if (net_state() == NET_UP) {
                set_status("Connected. Pinging...");
                /* First frame on the wire: everything the proxy sends
                   after it is shaped for this machine rather than for a
                   6510 (see send_hello). */
                send_hello();
                send_options();
                send_frame(MSG_PING, NULL, 0);
                /* Fetch the menu now so F1 has something in it. */
                send_frame(MSG_GET_MENU, NULL, 0);
            } else {
                set_status(err);
                say(2, err);
            }
        } else if (event == NET_EV_READ) {
            pump_socket();
        } else if (event == NET_EV_CLOSE) {
            print_abort();
            img_abort();
            mid_abort();
            set_status(err);
            say(2, "Disconnected.");
        }
        return 0;
    }

    case MM_MCINOTIFY:
        /* The tune ran out. Background music loops until told
           otherwise - the same contract the SID player has. */
        if (wParam == MCI_NOTIFY_SUCCESSFUL && g_mus_state == 1
                && g_mus_opened) {
            mciSendString("seek llm64mid to start", NULL, 0, NULL);
            mciSendString("play llm64mid notify", NULL, 0, hwnd);
        }
        return 0;

    /* A 256-colour driver arbitrates the hardware palette through
       these; the picture's colours are the only ones we bargain for.
       On a deep display neither ever matters. */
    case WM_QUERYNEWPALETTE:
        if (g_pic_hpal && g_pic_wnd) {
            HDC dc = GetDC(g_pic_wnd);
            HPALETTE old = SelectPalette(dc, g_pic_hpal, FALSE);
            UINT n = RealizePalette(dc);
            SelectPalette(dc, old, FALSE);
            ReleaseDC(g_pic_wnd, dc);
            if (n)
                InvalidateRect(g_pic_wnd, NULL, TRUE);
            return n;
        }
        break;

    case WM_PALETTECHANGED:
        if ((HWND)wParam != hwnd && g_pic_hpal && g_pic_wnd)
            InvalidateRect(g_pic_wnd, NULL, TRUE);
        break;

    case WM_COMMAND:
        switch (LLM_CMD_ID(wParam, lParam)) {
        case IDM_CONNECT:    do_connect(); return 0;
        case IDM_DISCONNECT: net_disconnect();
                             set_status("Disconnected."); return 0;
        case IDM_PING:       send_frame(MSG_PING, NULL, 0);
                             set_status("Ping sent."); return 0;
        case IDM_NEWCONV:    new_conversation(); return 0;
        case IDM_CANCEL:     send_frame(MSG_CANCEL_REQUEST, NULL, 0);
                             input_enable(1); return 0;
        case IDM_ABOUT:
            MessageBox(hwnd,
                       "LLM64 for Windows\n\n"
                       "A Windows 3.1 client for the LLM64 proxy.\n"
                       "Phase 1.",
                       "About LLM64", MB_OK | MB_ICONINFORMATION);
            return 0;
        case IDM_EXIT:
            PostMessage(hwnd, WM_CLOSE, 0, 0L);
            return 0;

        case IDM_SERVER:
            server_dialog(hwnd);
            return 0;

        case IDM_PICSET:
            pics_dialog(hwnd);
            return 0;

        case IDM_CONVS:
            if (conv_dialog(hwnd) == 2)
                new_conversation();
            return 0;

        case IDM_MENU: {
            HINSTANCE inst = LLM_INST(hwnd);
            FARPROC fn;
            int r;

            /* Ask again if we have nothing: the menu changes with the
               mode, so a stale one is worse than a late one. */
            if (net_state() == NET_UP)
                send_frame(MSG_GET_MENU, NULL, 0);
            fn = MakeProcInstance((FARPROC)MenuDlgProc, inst);
            r = DialogBox(inst, "LLM64ACTIONS", hwnd, (DLGPROC)fn);
            FreeProcInstance(fn);
            if (r == 1)
                menu_run(hwnd, g_menu_choice);
            return 0;
        }

        case IDM_NEWWINDOW:
            /* Closing the last document leaves an empty workspace, as
               it should in an MDI app - this is the way back. The
               transcript outlived the window, so it reappears with it. */
            if (g_conv)
                SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_conv, 0L);
            else
                g_conv = conv_create(hwnd);
            return 0;

        case IDM_PICTURE:
            /* Bring the picture back if it was closed, or to the front
               if it is buried. */
            if (g_pic_wnd)
                SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_pic_wnd, 0L);
            else
                pic_open();
            return 0;

        case IDM_DEFLAYOUT:
            if (!g_pic_wnd)
                pic_open();
            g_user_arranged = 0;
            layout_default();
            return 0;

        case IDM_SAVEPIC:
            do_save_pic(hwnd);
            return 0;

        case IDM_THEME_PAPER:
        case IDM_THEME_SCREEN:
            theme_apply(wParam == IDM_THEME_SCREEN ? THEME_SCREEN
                                                   : THEME_PAPER);
            save_ini();
            return 0;

        case IDM_CLOSEPAPER:
            /* Was "close all printout windows", when each sheet had one.
               There is one window now, so the useful verb is emptying it:
               the sheets are the memory, not the window. */
            note_clear();
            set_status("Notebook emptied.");
            return 0;

        case IDM_CASCADE: SendMessage(g_mdi, WM_MDICASCADE, 0, 0L); return 0;
        case IDM_TILE:    SendMessage(g_mdi, WM_MDITILE, 0, 0L); return 0;
        case IDM_ARRANGE: SendMessage(g_mdi, WM_MDIICONARRANGE, 0, 0L); return 0;
        }
        /* The launcher's buttons are children of the frame, so their
           clicks arrive here. Each toggles its window; the transcript,
           the shelf and the menu all outlive their windows, so closing
           costs nothing but the pixels. */
        if (wParam >= IDC_LAUNCHBASE
                && wParam < IDC_LAUNCHBASE + LAUNCH_N) {
            switch ((int)(wParam - IDC_LAUNCHBASE)) {
            case 0:
                /* The Menu button is the F1 dialog, not a toggle.
                   Posted rather than called: the modal box should open
                   after this WM_COMMAND finishes, not inside it. */
                PostMessage(hwnd, WM_COMMAND, IDM_MENU, 0L);
                return 0;
            case 1:
                if (g_conv)
                    SendMessage(g_mdi, WM_MDIDESTROY, (WPARAM)g_conv, 0L);
                else
                    g_conv = conv_create(hwnd);
                break;
            case 2:
                if (g_pic_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_pic_wnd, 0L);
                else
                    pic_open();
                break;
            case 3:
                /* The Music window floats over the desk rather than
                   claiming a column - it is controls, not a document. */
                if (g_mus_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_mus_wnd, 0L);
                else
                    mus_open_wnd();
                break;
            case 4:
                if (g_chr_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_chr_wnd, 0L);
                else
                    /* Taller than it was: the sheet holds a whole
                       character now, not four gauges. */
                    sheet_open(CHR_CLASS, &g_chr_wnd, DESK_CHR,
                               80, 30, 300, 340);
                break;
            case 5:
                if (g_inv_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_inv_wnd, 0L);
                else
                    sheet_open(INV_CLASS, &g_inv_wnd, DESK_INV,
                               130, 70, 220, 190);
                break;
            case 6:
                if (g_note_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_note_wnd, 0L);
                else
                    note_open();
                break;
            case 7:
                if (g_map_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_map_wnd, 0L);
                else
                    map_open();
                break;
            }
            if (!g_user_arranged)
                layout_default();
            /* Answer the click immediately rather than waiting for the
               tick: the button IS the feedback. */
            launch_sync();
            /* The click parked the focus on a toolbar button; give it
               back to something that can type. */
            if (g_input)
                SetFocus(g_input);
            return 0;
        }
        /* Anything else is either a document window being picked off the
           Window menu or a control notification: both belong to
           DefFrameProc, and swallowing them is how an MDI app quietly
           loses its Window menu. */
        break;

    case WM_DESTROY: {
        int i;
        KillTimer(hwnd, ID_LAUNCHTIMER);
        if (g_stipple) {
            DeleteObject(g_stipple);
            g_stipple = NULL;
        }
        net_shutdown();
        /* From 1: g_fonts[0] is the stock fixed font and is not ours
           to delete. */
        {
            unsigned i;
            for (i = 1; i < FONT_VARIANTS; i++)
                if (g_fonts[i])
                    DeleteObject(g_fonts[i]);
        }
        if (g_bg_brush)
            DeleteObject(g_bg_brush);
        if (g_pic_hpal)
            DeleteObject(g_pic_hpal);
        if (g_pic_mem)
            GlobalFree(g_pic_mem);
        if (g_img_mem)
            GlobalFree(g_img_mem);
        shelf_clear();
        mus_mci_close();
        if (g_mid_mem)
            GlobalFree(g_mid_mem);
        if (g_mus_file[0]) {
            OFSTRUCT of;
            OpenFile(g_mus_file, &of, OF_DELETE);
        }
        for (i = 0; i < MAX_PAPER; i++)
            if (g_paper[i].live)
                sb_free(&g_paper[i].sb);
        sb_free(&g_conv_view.sb);
        PostQuitMessage(0);
        return 0;
    }
    }
    return DefFrameProc(hwnd, g_mdi, msg, wParam, lParam);
}

/* Where LLM64.INI is, in full. A bare filename does not mean "next to
   the program": the profile calls resolve an unqualified name against
   the Windows directory, which is the last place someone running this
   off a floppy or out of a folder would think to put it. So derive the
   path from the module's own. */
static void ini_path(HINSTANCE hInst)
{
    char *p;

    if (GetModuleFileName(hInst, g_ini, sizeof(g_ini) - 16) <= 0) {
        lstrcpy(g_ini, INI_FILE);
        return;
    }
    p = g_ini + lstrlen(g_ini);
    while (p > g_ini && *(p - 1) != '\\' && *(p - 1) != ':')
        p--;
    lstrcpy(p, INI_FILE);
}

static void load_ini(void)
{
    char buf[32];

    GetPrivateProfileString("Server", "Host", "127.0.0.1",
                            g_host, sizeof(g_host), g_ini);
    g_port = GetPrivateProfileInt("Server", "Port", 6400, g_ini);
    GetPrivateProfileString("Display", "Theme", "paper",
                            buf, sizeof(buf), g_ini);
    g_theme = (buf[0] == 's' || buf[0] == 'S') ? THEME_SCREEN : THEME_PAPER;
    g_room_pics = GetPrivateProfileInt("Pictures", "EveryRoom", 0, g_ini)
        ? 1 : 0;
}

static void save_ini(void)
{
    char num[16];

    WritePrivateProfileString("Server", "Host", g_host, g_ini);
    wsprintf(num, "%u", g_port);
    WritePrivateProfileString("Server", "Port", num, g_ini);
    WritePrivateProfileString("Display", "Theme",
                              g_theme == THEME_SCREEN ? "screen" : "paper",
                              g_ini);
    WritePrivateProfileString("Pictures", "EveryRoom",
                              g_room_pics ? "1" : "0", g_ini);
}

int PASCAL WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR cmdline, int show)
{
    WNDCLASS wc;
    HWND hwnd;
    HANDLE accel;
    MSG msg;
    char *p;

    ini_path(hInst);
    load_ini();
    /* "LLM64 host port" on the command line beats the INI - it is how
       the test harness points the client at a scratch proxy. */
    if (cmdline && *cmdline) {
        lstrcpyn(g_host, cmdline, sizeof(g_host) - 1);
        p = g_host;
        while (*p && *p != ' ') p++;
        if (*p == ' ') {
            *p++ = '\0';
            g_port = (unsigned)atoi(p);
        }
    }

    if (!hPrev) {
        wc.style = CHROME_CLASS_STYLE;
        wc.lpfnWndProc = FrameProc;
        wc.cbClsExtra = 0;
        wc.cbWndExtra = 0;
        wc.hInstance = hInst;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hCursor = LoadCursor(NULL, IDC_ARROW);
        wc.hbrBackground = GetStockObject(LTGRAY_BRUSH);
        wc.lpszMenuName = "LLM64MENU";
        wc.lpszClassName = APP_CLASS;
        if (!RegisterClass(&wc))
            return 1;

        /* Document windows. No class background: the conversation is
           covered by its pane and its input box, and painting grey
           under them only buys a flash on resize. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = ConvProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = GetStockObject(LTGRAY_BRUSH);
        wc.lpszMenuName = NULL;
        wc.lpszClassName = CONV_CLASS;
        if (!RegisterClass(&wc))
            return 1;

        /* The Notebook: the printed sheets, indexed. No class background:
           note_paint covers what its children do not. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = NoteProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = NULL;
        wc.lpszMenuName = NULL;
        wc.lpszClassName = NOTE_CLASS;
        if (!RegisterClass(&wc))
            return 1;

        /* The Map: drawn, not printed - map_paint owns every pixel. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = MapProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = NULL;
        wc.lpszMenuName = NULL;
        wc.lpszClassName = MAP_CLASS;
        if (!RegisterClass(&wc))
            return 1;

        /* The picture. No class background: pic_paint covers every
           pixel itself, empty state included. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = PicProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = NULL;
        wc.lpszMenuName = NULL;
        wc.lpszClassName = PIC_CLASS;
        if (!RegisterClass(&wc))
            return 1;


        /* The Music window: playback controls for the MIDI pipeline. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = MusProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = GetStockObject(LTGRAY_BRUSH);
        wc.lpszMenuName = NULL;
        wc.lpszClassName = MUS_CLASS;
        if (!RegisterClass(&wc))
            return 1;

        /* The sheet windows: the STATE block's two sidebars. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = ChrProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = GetStockObject(LTGRAY_BRUSH);
        wc.lpszMenuName = NULL;
        wc.lpszClassName = CHR_CLASS;
        if (!RegisterClass(&wc))
            return 1;
        wc.lpfnWndProc = InvProc;
        wc.lpszClassName = INV_CLASS;
        if (!RegisterClass(&wc))
            return 1;

        /* The pane. Four extra bytes because every pane carries a far
           pointer to the View it is drawing, which is what lets one
           window procedure serve the transcript and every sheet. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = PaneProc;
        wc.cbWndExtra = sizeof(View FAR *);
        wc.hIcon = NULL;
        wc.hbrBackground = NULL;
        wc.lpszMenuName = NULL;
        wc.lpszClassName = PANE_CLASS;
        if (!RegisterClass(&wc))
            return 1;
        wc.cbWndExtra = 0;
    }

    /* WS_CLIPCHILDREN is not decoration on an MDI frame: without it the
       frame's own erase - its class brush is light grey - is not clipped
       away from the windows sitting in its client area, so every relayout
       that exposes a strip of frame painted grey over whatever was there,
       and an MDI child's CAPTION only came back when that child's
       non-client area happened to be invalidated. The symptom was grey
       slabs where titlebars belong, most visibly during a conversation
       load. */
    hwnd = CreateWindow(APP_CLASS, APP_TITLE, CHROME_STYLE,
                        CW_USEDEFAULT, CW_USEDEFAULT, 640, 440,
                        NULL, NULL, hInst, NULL);
    if (!hwnd)
        return 1;
    /* One forced NC recalculation. Windows worked the client rect out at
       creation, before the frame knew it had no non-client area, and
       nothing recomputes it on its own - so without this the layout is a
       frame's width out and the MDI client covers the chrome. */
    {
        RECT wr;

        GetWindowRect(hwnd, &wr);
        SetWindowPos(hwnd, NULL, 0, 0, wr.right - wr.left,
                     wr.bottom - wr.top,
                     SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED);
    }
    ShowWindow(hwnd, show);
    UpdateWindow(hwnd);

    /* F1/F2/F3. The menu has advertised F2 and F3 since the spike with
       no accelerator table behind them, so they never worked. */
    /* The table loads under Wine and its keys do nothing there, the
       same way TranslateMDISysAccel does nothing there: Wine's 16-bit
       layer does not do accelerators. Ctrl+F4 proved that pattern on
       real Windows, so F1/F2/F3 are expected to work on a real machine
       and to stay dead under the emulator. Every one of them is also on
       a menu, which works everywhere. */
    accel = LoadAccelerators(hInst, "LLM64ACC");

    while (GetMessage(&msg, NULL, 0, 0)) {
        /* Ctrl+F4, Ctrl+F6 and the rest of the MDI system accelerators
           are the document windows', and they have to be offered the
           message before the frame translates it. */
        if (!TranslateMDISysAccel(g_mdi, &msg)
            && !(accel && TranslateAccelerator(hwnd, accel, &msg))) {
            TranslateMessage(&msg);
            DispatchMessage(&msg);
        }
    }
    return msg.wParam;
}
