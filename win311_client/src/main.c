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
#include <i86.h>        /* FP_OFF, for segment-boundary math */
#include "wire.h"
#include "net.h"
#include "scroll.h"
#include "resource.h"

#define APP_CLASS   "LLM64Main"
#define CONV_CLASS  "LLM64Conv"
#define PAPER_CLASS "LLM64Paper"
#define PIC_CLASS   "LLM64Pic"
#define ACT_CLASS   "LLM64Act"
#define MUS_CLASS   "LLM64Mus"
#define CHR_CLASS   "LLM64Chr"
#define INV_CLASS   "LLM64Inv"
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
   ASCII, so several can be on the desk at once - which is the whole
   reason for a workspace with documents in it. The cap is memory
   honesty, not taste: each sheet holds its own far blocks. */
#define MAX_PAPER   4

static View     g_conv_view;
static View     g_paper[MAX_PAPER];
static HWND     g_paper_wnd[MAX_PAPER];
static unsigned g_paper_seq;        /* sheets printed, for the titles */

static HWND     g_frame;    /* the one top-level window */
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
static FARPROC  g_old_edit_proc;
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

/* A free slot, or the oldest sheet's - four on the desk at once is
   plenty, and the fifth print job is a better use of the memory than
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
    if (g_paper_wnd[oldest])
        SendMessage(g_mdi, WM_MDIDESTROY, (WPARAM)g_paper_wnd[oldest], 0L);
    if (g_paper[oldest].live) {     /* if the window did not take it */
        sb_free(&g_paper[oldest].sb);
        g_paper[oldest].live = 0;
        g_paper[oldest].pane = NULL;
    }
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
    if (g_paper[g_prt_slot].live && !g_paper_wnd[g_prt_slot]) {
        sb_free(&g_paper[g_prt_slot].sb);
        g_paper[g_prt_slot].live = 0;
    }
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
                   | CAP_DIB_IMAGES | CAP_MIDI | CAP_STATE_JSON)

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

/* The Actions window: the server-fed menu as a column of buttons in an
   MDI document of its own. It used to be a panel glued to the frame's
   right edge, permanently spending 164 pixels of a 640-wide screen -
   the scarcest resource this program has. As a document it opens and
   closes like everything else, from the launcher or the Window menu,
   and the INI remembers the choice. */
#define ACT_W       164
#define ACT_PAD     6

static HWND g_act_wnd;
static HWND g_act_btn[MAX_MENU];
static int  g_act_count;
static int  g_act_open;                 /* open at startup (INI) */
static char g_act_sig[MAX_MENU + 2];    /* what the buttons were built from */
static int  g_quitting;                 /* app teardown, not a user close */

static void act_layout(HWND hwnd)
{
    RECT rc;
    int i, y, bh, avail;

    if (!g_act_count)
        return;
    GetClientRect(hwnd, &rc);
    avail = (int)rc.bottom - ACT_PAD * 2;
    /* Squeeze rather than overflow: a thirteen-entry menu on a 480-line
       screen has less room per button than a four-entry one. */
    bh = (g_ch + 12);
    if (bh * g_act_count > avail)
        bh = avail / g_act_count;
    if (bh < 14) bh = 14;
    y = ACT_PAD;
    for (i = 0; i < g_act_count; i++) {
        MoveWindow(g_act_btn[i], ACT_PAD, y,
                   (int)rc.right - ACT_PAD * 2, bh - 2, TRUE);
        y += bh;
    }
}

/* (Re)build the buttons inside the Actions window, but only when the
   menu actually changed - it is re-fetched every time the F1 box opens,
   and tearing down a column of buttons to build the identical one back
   flickers for nothing. */
static void act_buttons(HWND hwnd)
{
    char sig[MAX_MENU + 2];
    HINSTANCE inst;
    int i;

    sig[0] = (char)g_menu_count;
    for (i = 0; i < g_menu_count && i < MAX_MENU; i++)
        sig[i + 1] = g_menu[i].key;
    sig[i + 1] = '\0';
    if (g_act_count && memcmp(sig, g_act_sig, (size_t)i + 2) == 0)
        return;
    memcpy(g_act_sig, sig, (size_t)i + 2);

    for (i = 0; i < g_act_count; i++)
        if (g_act_btn[i]) {
            DestroyWindow(g_act_btn[i]);
            g_act_btn[i] = NULL;
        }
    g_act_count = 0;
    inst = (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE);
    for (i = 0; i < g_menu_count && i < MAX_MENU; i++) {
        g_act_btn[i] = CreateWindow("BUTTON", g_menu[i].label,
                                    WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                                    0, 0, 10, 10, hwnd,
                                    (HMENU)(IDC_BARBASE + i), inst, NULL);
        if (!g_act_btn[i])
            break;
    }
    g_act_count = i;
    act_layout(hwnd);
}

/* The launcher: a row of rectangular buttons across the top of the
   frame, one per big window, click to open or close - the way a 1993
   program let you see its rooms without memorizing its menus. Owned by
   the frame like the status strip, because it reports on the desk as a
   whole. */
#define LAUNCH_N 7

static HWND g_launch[LAUNCH_N];

static int launch_h(void)
{
    return g_ch + 14;
}

static void launch_create(HWND frame)
{
    static const char *label[LAUNCH_N] =
        { "Menu", "Conversation", "Picture", "Actions", "Music",
          "Character", "Items" };
    HINSTANCE inst = (HINSTANCE)GetWindowWord(frame, GWW_HINSTANCE);
    int i;

    for (i = 0; i < LAUNCH_N; i++)
        g_launch[i] = CreateWindow("BUTTON", label[i],
                                   WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                                   0, 0, 10, 10, frame,
                                   (HMENU)(IDC_LAUNCHBASE + i), inst,
                                   NULL);
}

static void launch_layout(HWND frame)
{
    static const int w[LAUNCH_N] = { 52, 104, 76, 72, 64, 80, 56 };
    int i, x = 4;

    (void)frame;
    for (i = 0; i < LAUNCH_N; i++) {
        if (g_launch[i])
            MoveWindow(g_launch[i], x, 3, w[i], launch_h() - 6, TRUE);
        x += w[i] + 4;
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

/* Open a window on a finished sheet. The slot travels in the MDI create
   struct's lParam because the child's WM_CREATE runs before
   WM_MDICREATE returns, so it cannot yet be found by its handle. */
static void paper_open(int slot)
{
    MDICREATESTRUCT mcs;
    char title[40];

    wsprintf(title, "Printout %u", g_paper_born[slot]);
    mcs.szClass = PAPER_CLASS;
    mcs.szTitle = title;
    mcs.hOwner  = (HINSTANCE)GetWindowWord(g_frame, GWW_HINSTANCE);
    /* Offset each sheet so a second one does not hide the first. */
    mcs.x       = 16 + (int)(g_paper_born[slot] % 4) * 20;
    mcs.y       = 8 + (int)(g_paper_born[slot] % 4) * 16;
    mcs.cx      = g_cw * 84;
    mcs.cy      = g_ch * 24;
    mcs.style   = 0;
    mcs.lParam  = (LONG)slot;

    g_paper_wnd[slot] = (HWND)(WORD)SendMessage(g_mdi, WM_MDICREATE, 0,
                                               (LONG)(LPMDICREATESTRUCT)&mcs);
}

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

    g_prt_slot     = slot;
    g_prt_active   = 1;
    g_prt_blocks   = 0;
    g_prt_total    = p[1];
    g_prt_formfeed = (p[0] & 2) ? 1 : 0;
    set_status("Printing...");
    send_frame(MSG_ACK, NULL, 0);
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
    for (i = 0; i < len; i++)
        sb_putc(&g_paper[g_prt_slot].sb, (char)p[i]);
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
    paper_open(slot);
    wsprintf(msg, "Printed %u lines to Printout %u.",
             (unsigned)sb_lines(&g_paper[slot].sb) - 1, g_paper_born[slot]);
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
static int g_in_layout;         /* our own MoveWindows are not "the user" */
static int g_layout_ready;      /* creation-time WM_SIZEs are not either */

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
    unsigned long size;
} g_shelf[MAX_SHELF];
static int  g_shelf_count;
static int  g_shelf_cur = -1;   /* index on display */
static HWND g_pic_lb;           /* the browser listbox, in the pic window */

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

/* Open (or refresh) the picture window. Created through the MDI client
   like every document; PicProc records the handle in WM_CREATE because
   this call has not returned yet when the first messages arrive. */
static void pic_open(void)
{
    MDICREATESTRUCT mcs;

    if (g_pic_wnd) {
        /* The browser list appears with the first shelf entry, and
           only a layout pass reveals it. */
        pic_layout(g_pic_wnd);
        InvalidateRect(g_pic_wnd, NULL, TRUE);
        return;
    }
    mcs.szClass = PIC_CLASS;
    mcs.szTitle = "Picture";
    mcs.hOwner  = (HINSTANCE)GetWindowWord(g_frame, GWW_HINSTANCE);
    mcs.x       = 24;
    mcs.y       = 16;
    mcs.cx      = 336;
    mcs.cy      = 240;
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
static void shelf_add(void)
{
    HFILE f;
    OFSTRUCT of;
    int i;

    if (!g_pic_mem)
        return;
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
    g_shelf[g_shelf_count].w    = g_pic_w;
    g_shelf[g_shelf_count].h    = g_pic_h;
    g_shelf[g_shelf_count].size = g_pic_size;
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
        OpenFile(g_shelf[i].path, &of, OF_DELETE);
    g_shelf_count = 0;
    g_shelf_cur = -1;
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
    long hp, maxhp, mana, maxmana, gold, score, xp, level;
    int  has_hp, has_mana, has_gold, has_score, has_xp, has_level;
    char location[64];
    char appearance[200];
    char companions[160];
    char inv[16][40];
    int  inv_n;
    int  valid;
} g_sheet;

static HWND g_chr_wnd;          /* the Character window, if open */
static HWND g_inv_wnd;          /* the Inventory window, if open */
static HWND g_inv_lb;

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
    js_str(j, "location",   g_sheet.location,   sizeof(g_sheet.location));
    js_str(j, "appearance", g_sheet.appearance, sizeof(g_sheet.appearance));
    g_sheet.inv_n = js_strarr(j, "inventory", g_sheet.inv, 16, NULL, 0);
    js_strarr(j, "companions", NULL, 0,
              g_sheet.companions, sizeof(g_sheet.companions));
    g_sheet.valid = g_sheet.has_hp || g_sheet.location[0]
        || g_sheet.appearance[0] || g_sheet.inv_n;
}

/* Refresh whatever sheet windows are open. Defined with the windows
   themselves, called from the wire. */
static void sheet_update(void);

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
        if (g_act_wnd)
            act_buttons(g_act_wnd);
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

    /* The narrator's bookkeeping, for the sheet windows. */
    case MSG_STATE_JSON:
        if (len >= 1 && p[len - 1] == 0) {
            sheet_parse((const char *)p);
            sheet_update();
        }
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
        switch (wParam) {
        case SB_LINEUP:   v->top--; break;
        case SB_LINEDOWN: v->top++; break;
        case SB_PAGEUP:   v->top -= rows; break;
        case SB_PAGEDOWN: v->top += rows; break;
        case SB_THUMBPOSITION:
        case SB_THUMBTRACK: v->top = LOWORD(lParam); break;
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
                          (HINSTANCE)GetWindowWord(parent, GWW_HINSTANCE),
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
static void layout_default(void)
{
    RECT rc;
    int pw, aw, mw, mh;

    if (!g_mdi)
        return;
    g_in_layout = 1;
    /* A maximized document owns the whole workspace, and MoveWindow on
       it is quietly ignored - restore first. */
    if (g_conv && IsZoomed(g_conv))
        SendMessage(g_mdi, WM_MDIRESTORE, (WPARAM)g_conv, 0L);
    if (g_pic_wnd && IsZoomed(g_pic_wnd))
        SendMessage(g_mdi, WM_MDIRESTORE, (WPARAM)g_pic_wnd, 0L);
    if (g_act_wnd && IsZoomed(g_act_wnd))
        SendMessage(g_mdi, WM_MDIRESTORE, (WPARAM)g_act_wnd, 0L);
    if (g_mus_wnd && IsZoomed(g_mus_wnd))
        SendMessage(g_mdi, WM_MDIRESTORE, (WPARAM)g_mus_wnd, 0L);
    GetClientRect(g_mdi, &rc);
    aw = g_act_wnd ? ACT_W : 0;
    /* The multiply is long on purpose: 892 pixels x 38 is already past
       what a 16-bit int holds, and the overflow made pw negative - a
       picture window with negative width is an invisible one. The old
       side panel kept the workspace narrow enough to hide this. */
    pw = g_pic_wnd
        ? (int)(((long)((int)rc.right - aw) * 38) / 100)
        : 0;
    /* Music tucks into the bottom-right corner: under the picture,
       stealing its column's bottom edge - or over the conversation's
       corner when there is no picture. Three text lines plus the
       button row plus its caption. */
    mh = g_mus_wnd ? g_ch * 4 + 56 : 0;
    if (mh > (int)rc.bottom / 2)
        mh = (int)rc.bottom / 2;
    mw = pw ? pw : 260;
    if (mw > (int)rc.right - aw)
        mw = (int)rc.right - aw;
    if (g_conv)
        MoveWindow(g_conv, 0, 0, (int)rc.right - pw - aw,
                   (int)rc.bottom, TRUE);
    if (g_pic_wnd)
        MoveWindow(g_pic_wnd, (int)rc.right - pw - aw, 0, pw,
                   (int)rc.bottom - mh, TRUE);
    if (g_mus_wnd)
        MoveWindow(g_mus_wnd, (int)rc.right - mw - aw,
                   (int)rc.bottom - mh, mw, mh, TRUE);
    if (g_act_wnd)
        MoveWindow(g_act_wnd, (int)rc.right - aw, 0, aw,
                   (int)rc.bottom, TRUE);
    g_in_layout = 0;
    g_layout_ready = 1;
}

/* The frame gives everything except the status strip to the MDI client,
   which is what actually owns the document windows. */
static void frame_layout(HWND hwnd)
{
    RECT rc;
    int statush = g_ch + 6;
    int h, w;

    if (!g_mdi)
        return;
    GetClientRect(hwnd, &rc);
    h = rc.bottom - statush - launch_h();
    if (h < g_ch) h = g_ch;
    /* The documents get everything between the launcher strip and the
       status strip. */
    w = (int)rc.right;
    MoveWindow(g_mdi, 0, launch_h(), w, h, TRUE);
    launch_layout(hwnd);
}

long FAR PASCAL _export ConvProc(HWND hwnd, UINT msg, UINT wParam,
                                 LONG lParam)
{
    HINSTANCE inst;

    switch (msg) {
    case WM_CREATE:
        inst = (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE);
        pane_create(hwnd, &g_conv_view);
        g_input = CreateWindow("EDIT", "",
                               WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL,
                               0, 0, 10, 10, hwnd, (HMENU)ID_INPUT, inst, NULL);
        SendMessage(g_input, WM_SETFONT, (WPARAM)g_font, 0L);
        g_old_edit_proc = (FARPROC)GetWindowLong(g_input, GWL_WNDPROC);
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
        if (wParam)
            SetFocus(g_input);
        break;

    case WM_CTLCOLOR:
        /* The input box has to follow the theme, or the Screen palette
           leaves a white box glued under a black transcript. WM_CTLCOLOR
           is how 3.1 did this - the brush is returned, not copied, which
           is why it is a global that outlives the message. */
        if (HIWORD(lParam) == CTLCOLOR_EDIT) {
            SetTextColor((HDC)wParam, g_pal[1]);
            SetBkColor((HDC)wParam, g_bg);
            return (LONG)g_bg_brush;
        }
        break;

    case WM_SETFOCUS:
        if (g_input)
            SetFocus(g_input);
        return 0;

    case WM_DESTROY:
        /* A document window can be closed - Ctrl+F4, the close box, the
           system menu. Forget its children rather than leaving handles
           that outlive the windows they name: an arriving CHAT_CHUNK
           would otherwise paint into a window that no longer exists.
           Window > New Conversation Window brings it back. */
        g_conv = NULL;
        g_conv_view.pane = NULL;
        g_input = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* ---------------------------------------------------------------- */
/* Paper                                                             */
/* ---------------------------------------------------------------- */

/* A sheet of printed paper. The proxy has already composed and laid the
   document out to a printer width (docs/14), so this window's whole job
   is to hold still and be read: no input box, no re-flow, a margin, and
   the text exactly where the printer would have put it. */
long FAR PASCAL _export PaperProc(HWND hwnd, UINT msg, UINT wParam,
                                  LONG lParam)
{
    RECT rc;
    int i;

    switch (msg) {
    case WM_CREATE: {
        /* The slot arrives in the MDI create struct: this runs before
           WM_MDICREATE returns, so the sheet cannot yet be found by
           looking its window up. */
        LPMDICREATESTRUCT mp =
            (LPMDICREATESTRUCT)((LPCREATESTRUCT)lParam)->lpCreateParams;
        int slot = (int)mp->lParam;

        if (slot < 0 || slot >= MAX_PAPER)
            return -1;
        g_paper_wnd[slot] = hwnd;
        pane_create(hwnd, &g_paper[slot]);
        break;
    }

    case WM_SIZE:
        GetClientRect(hwnd, &rc);
        for (i = 0; i < MAX_PAPER; i++) {
            if (g_paper_wnd[i] == hwnd && g_paper[i].pane) {
                MoveWindow(g_paper[i].pane, 0, 0, rc.right, rc.bottom, TRUE);
                break;
            }
        }
        break;

    case WM_MDIACTIVATE:
        /* Paper has nothing to type into, so activating a sheet must not
           leave the keyboard pointing at the conversation's input box -
           give the focus to the sheet's own pane, where PgUp and PgDn
           work. */
        if (wParam) {
            for (i = 0; i < MAX_PAPER; i++) {
                if (g_paper_wnd[i] == hwnd && g_paper[i].pane) {
                    SetFocus(g_paper[i].pane);
                    break;
                }
            }
        }
        break;

    case WM_DESTROY:
        for (i = 0; i < MAX_PAPER; i++) {
            if (g_paper_wnd[i] == hwnd) {
                /* The sheet is thrown away with its window: unlike the
                   transcript, nothing later appends to it, and its far
                   blocks are worth more back on the heap. */
                if (g_paper[i].live)
                    sb_free(&g_paper[i].sb);
                g_paper[i].live = 0;
                g_paper[i].pane = NULL;
                g_paper_wnd[i] = NULL;
                if (g_prt_active && g_prt_slot == i)
                    g_prt_active = 0;
                break;
            }
        }
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* ---------------------------------------------------------------- */
/* The picture window                                                */
/* ---------------------------------------------------------------- */

/* Rows the browser list takes from the bottom of the picture window.
   Zero until there is something to browse - a picture on its own
   deserves the whole window. */
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

    if (!g_pic_lb)
        return;
    GetClientRect(hwnd, &rc);
    if (lh) {
        MoveWindow(g_pic_lb, 0, rc.bottom - lh, rc.right, lh, TRUE);
        ShowWindow(g_pic_lb, SW_SHOW);
    } else {
        ShowWindow(g_pic_lb, SW_HIDE);
    }
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
    /* The browser list owns the bottom band; paint only above it. */
    rc.bottom -= pic_list_h(hwnd);
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
    int i;

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
                                (HINSTANCE)GetWindowWord(hwnd,
                                                         GWW_HINSTANCE),
                                NULL);
        for (i = 0; i < g_shelf_count; i++)
            SendMessage(g_pic_lb, LB_ADDSTRING, 0,
                        (LONG)(LPSTR)(g_shelf[i].title[0]
                                      ? g_shelf[i].title
                                      : (char *)"(untitled)"));
        if (g_shelf_cur >= 0)
            SendMessage(g_pic_lb, LB_SETCURSEL, g_shelf_cur, 0L);
        break;

    case WM_PAINT:
        pic_paint(hwnd);
        return 0;

    case WM_ERASEBKGND:
        /* pic_paint covers every pixel; erasing first only flickers. */
        return 1;

    case WM_COMMAND:
        if (wParam == ID_PICLIST && HIWORD(lParam) == LBN_SELCHANGE) {
            shelf_show((int)SendMessage(g_pic_lb, LB_GETCURSEL, 0, 0L));
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
        g_pic_wnd = NULL;
        g_pic_lb  = NULL;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

/* ---------------------------------------------------------------- */
/* The Actions window                                                */
/* ---------------------------------------------------------------- */

static void menu_run(HWND owner, int idx);

long FAR PASCAL _export ActProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    switch (msg) {
    case WM_CREATE:
        /* Recorded early, like the picture window and for the same
           reason: messages arrive before WM_MDICREATE returns. */
        g_act_wnd = hwnd;
        act_buttons(hwnd);
        break;

    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        act_layout(hwnd);
        break;

    case WM_COMMAND:
        if (wParam >= IDC_BARBASE && wParam < IDC_BARBASE + MAX_MENU) {
            menu_run(g_frame, (int)(wParam - IDC_BARBASE));
            return 0;
        }
        break;

    case WM_DESTROY:
        g_act_wnd = NULL;
        g_act_count = 0;
        g_act_sig[0] = '\0';
        /* Closing the window with its close box is choosing to not have
           it; remember that. App teardown is not a choice. */
        if (!g_quitting)
            g_act_open = 0;
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

static void act_open_wnd(void)
{
    MDICREATESTRUCT mcs;

    if (g_act_wnd)
        return;
    mcs.szClass = ACT_CLASS;
    mcs.szTitle = "Actions";
    mcs.hOwner  = (HINSTANCE)GetWindowWord(g_frame, GWW_HINSTANCE);
    mcs.x       = 40;
    mcs.y       = 24;
    mcs.cx      = ACT_W;
    mcs.cy      = 300;
    mcs.style   = 0;
    mcs.lParam  = 0;
    /* Same guard as pic_open: a window's own birth is not the user
       arranging the desk. */
    g_in_layout = 1;
    SendMessage(g_mdi, WM_MDICREATE, 0, (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
    if (g_conv)
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_conv, 0L);
}

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
    int i, bw, bh = g_ch + 10, x;

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
    static const char *label[3] = { "Pause", "Stop", "Next" };
    HINSTANCE inst;
    int i;

    switch (msg) {
    case WM_CREATE:
        g_mus_wnd = hwnd;
        inst = (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE);
        for (i = 0; i < 3; i++)
            g_mus_btn[i] = CreateWindow("BUTTON", label[i],
                                        WS_CHILD | WS_VISIBLE
                                        | BS_PUSHBUTTON,
                                        0, 0, 10, 10, hwnd,
                                        (HMENU)(IDC_MUSBASE + i), inst,
                                        NULL);
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
        switch (wParam) {
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
        }
        break;

    case WM_DESTROY:
        g_mus_wnd = NULL;
        for (i = 0; i < 3; i++)
            g_mus_btn[i] = NULL;
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

static void chr_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc, br;
    char line[120];
    int y = 6, bh;

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);
    SetBkMode(hdc, TRANSPARENT);
    SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SetTextColor(hdc, RGB(0, 0, 0));
    if (!g_sheet.valid) {
        DrawText(hdc, "No adventure state yet.\n\nStart an adventure "
                 "and the narrator's own bookkeeping appears here.",
                 -1, &rc, DT_CENTER | DT_WORDBREAK | DT_NOPREFIX);
        EndPaint(hwnd, &ps);
        return;
    }
    if (g_sheet.location[0]) {
        wsprintf(line, "At: %s", (LPSTR)g_sheet.location);
        TextOut(hdc, 8, y, line, lstrlen(line));
        y += g_ch + 4;
    }
    if (g_sheet.has_hp) {
        /* The HP bar every sidebar had: sunken frame, red when the
           narrator says you should be worried. */
        bh = g_ch;
        wsprintf(line, "HP %ld / %ld", g_sheet.hp, g_sheet.maxhp);
        TextOut(hdc, 8, y, line, lstrlen(line));
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
        y += bh + 4;
    }
    if (g_sheet.has_mana && g_sheet.maxmana > 0) {
        wsprintf(line, "Mana %ld / %ld", g_sheet.mana, g_sheet.maxmana);
        TextOut(hdc, 8, y, line, lstrlen(line));
        y += g_ch + 2;
    }
    line[0] = '\0';
    if (g_sheet.has_gold)
        wsprintf(line, "Gold %ld   ", g_sheet.gold);
    if (g_sheet.has_level)
        wsprintf(line + lstrlen(line), "Level %ld   ", g_sheet.level);
    if (g_sheet.has_xp)
        wsprintf(line + lstrlen(line), "XP %ld   ", g_sheet.xp);
    if (g_sheet.has_score)
        wsprintf(line + lstrlen(line), "Score %ld", g_sheet.score);
    if (line[0]) {
        TextOut(hdc, 8, y, line, lstrlen(line));
        y += g_ch + 4;
    }
    if (g_sheet.appearance[0]) {
        RECT tr = rc;
        tr.left = 8; tr.top = y; tr.right -= 8;
        DrawText(hdc, g_sheet.appearance, -1, &tr,
                 DT_WORDBREAK | DT_NOPREFIX | DT_CALCRECT);
        DrawText(hdc, g_sheet.appearance, -1, &tr,
                 DT_WORDBREAK | DT_NOPREFIX);
        y = (int)tr.bottom + 4;
    }
    if (g_sheet.companions[0]) {
        RECT tr = rc;
        tr.left = 8; tr.top = y; tr.right -= 8;
        wsprintf(line, "With you: %s", (LPSTR)g_sheet.companions);
        DrawText(hdc, line, -1, &tr, DT_WORDBREAK | DT_NOPREFIX);
    }
    EndPaint(hwnd, &ps);
}

long FAR PASCAL _export ChrProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    switch (msg) {
    case WM_CREATE:
        g_chr_wnd = hwnd;
        break;
    case WM_PAINT:
        chr_paint(hwnd);
        return 0;
    case WM_SIZE:
    case WM_MOVE:
        if (g_layout_ready && !g_in_layout)
            g_user_arranged = 1;
        InvalidateRect(hwnd, NULL, TRUE);
        break;
    case WM_DESTROY:
        g_chr_wnd = NULL;
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
        char title[40];
        if (g_sheet.inv_n)
            wsprintf(title, "Inventory (%d)", g_sheet.inv_n);
        else
            lstrcpy(title, "Inventory");
        SetWindowText(g_inv_wnd, title);
    }
}

long FAR PASCAL _export InvProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    RECT rc;

    switch (msg) {
    case WM_CREATE:
        g_inv_wnd = hwnd;
        g_inv_lb = CreateWindow("LISTBOX", NULL,
                                WS_CHILD | WS_VISIBLE | WS_BORDER
                                | WS_VSCROLL,
                                0, 0, 10, 10, hwnd, (HMENU)ID_INVLIST,
                                (HINSTANCE)GetWindowWord(hwnd,
                                                         GWW_HINSTANCE),
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

static void sheet_open(const char *cls, HWND *slot, int x, int y,
                       int cx, int cy)
{
    MDICREATESTRUCT mcs;

    if (*slot) {
        SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)*slot, 0L);
        return;
    }
    mcs.szClass = cls;
    mcs.szTitle = cls[5] == 'C' ? "Character" : "Inventory";
    mcs.hOwner  = (HINSTANCE)GetWindowWord(g_frame, GWW_HINSTANCE);
    mcs.x = x; mcs.y = y; mcs.cx = cx; mcs.cy = cy;
    mcs.style = 0;
    mcs.lParam = 0;
    g_in_layout = 1;
    SendMessage(g_mdi, WM_MDICREATE, 0, (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
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
    mcs.hOwner  = (HINSTANCE)GetWindowWord(g_frame, GWW_HINSTANCE);
    mcs.x       = 60;
    mcs.y       = 40;
    mcs.cx      = 250;
    mcs.cy      = 140;
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
    GetClientRect(hwnd, &rc);
    sr = rc;
    sr.top = rc.bottom - statush;
    FillRect(hdc, &sr, GetStockObject(LTGRAY_BRUSH));
    /* The sunken top edge every 3.1 status strip had */
    MoveTo(hdc, sr.left, sr.top);
    LineTo(hdc, sr.right, sr.top);
    /* And the same edge under the launcher strip, so it reads as a
       toolbar rather than as buttons loose on the background. */
    MoveTo(hdc, rc.left, launch_h() - 1);
    LineTo(hdc, rc.right, launch_h() - 1);
    SetBkMode(hdc, TRANSPARENT);
    SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SetTextColor(hdc, RGB(0, 0, 0));
    TextOut(hdc, 4, sr.top + 3, g_status, lstrlen(g_status));
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
    int i, rows, cols, x, y, w, h;
    /* mgy clears the caption line above the buttons - the static text is
       at 6 dialog units, which is lower than it looks. */
    int bw = 190, bh = 26, gap = 6, mgx = 12, mgy = 34;
    HINSTANCE inst;
    RECT wr, cr, fr;
    char text[52];

    (void)lParam;
    switch (msg) {
    case WM_INITDIALOG:
        inst = (HINSTANCE)GetWindowWord(dlg, GWW_HINSTANCE);
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
        return TRUE;

    case WM_COMMAND:
        if (wParam >= IDC_MENUBASE && wParam < IDC_MENUBASE + MAX_MENU) {
            g_menu_choice = (int)(wParam - IDC_MENUBASE);
            EndDialog(dlg, 1);
            return TRUE;
        }
        if (wParam == IDCANCEL) {
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
    (void)lParam;
    switch (msg) {
    case WM_INITDIALOG:
        CheckDlgButton(dlg, IDC_ROOMPICS, g_room_pics);
        return TRUE;

    case WM_COMMAND:
        switch (wParam) {
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
    HINSTANCE inst = (HINSTANCE)GetWindowWord(owner, GWW_HINSTANCE);
    FARPROC fn = MakeProcInstance((FARPROC)PicsDlgProc, inst);
    int r = DialogBox(inst, "LLM64PICS", owner, (DLGPROC)fn);

    FreeProcInstance(fn);
    if (r == 1 && net_state() == NET_UP) {
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
    int i;
    char q[96];

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
        if (wParam == IDC_CONVLIST && HIWORD(lParam) == LBN_DBLCLK)
            wParam = IDC_CONVLOAD;
        switch (wParam) {
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
    HINSTANCE inst = (HINSTANCE)GetWindowWord(owner, GWW_HINSTANCE);
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
    char buf[64];
    unsigned port;

    (void)lParam;
    switch (msg) {
    case WM_INITDIALOG:
        SetDlgItemText(dlg, IDC_HOST, g_host);
        SetDlgItemInt(dlg, IDC_PORT, g_port, FALSE);
        CheckDlgButton(dlg, IDC_RECONNECT, 1);
        return TRUE;

    case WM_COMMAND:
        switch (wParam) {
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
    HINSTANCE inst = (HINSTANCE)GetWindowWord(owner, GWW_HINSTANCE);
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
    mcs.hOwner  = (HINSTANCE)GetWindowWord(frame, GWW_HINSTANCE);
    /* A real restored size, not CW_USEDEFAULT, and created *unmaximized*
       even though it is wanted maximized. Creating it maximized with
       CW_USEDEFAULT leaves the normal rect degenerate, so the first
       un-maximize restores the window to no area at all - squashed flat
       on Windows, and gone entirely under Wine. Maximizing afterwards
       records this rect as the one to come back to. */
    mcs.x       = 8;
    mcs.y       = 8;
    mcs.cx      = g_cw * 80 + 6 * GetSystemMetrics(SM_CXVSCROLL);
    mcs.cy      = g_ch * 24;
    mcs.style   = 0;
    mcs.lParam  = 0;

    /* Same guard as pic_open: creation-time WM_SIZEs are not the user
       arranging the desk. */
    g_in_layout = 1;
    w = (HWND)(WORD)SendMessage(g_mdi, WM_MDICREATE, 0,
                                (LONG)(LPMDICREATESTRUCT)&mcs);
    g_in_layout = 0;
    /* Not maximized any more: the desk holds two documents now, and
       layout_default puts this one beside the picture. Maximizing is
       one double-click away for anyone who wants the old look. */
    return w;
}

/* Open or close the Actions window - from the launcher, the Window
   menu, or (as IDM_SHOWBAR) the same accelerator the old panel had. */
static void act_toggle(HWND frame)
{
    if (g_act_wnd) {
        SendMessage(g_mdi, WM_MDIDESTROY, (WPARAM)g_act_wnd, 0L);
        g_act_open = 0;
    } else {
        act_open_wnd();
        g_act_open = g_act_wnd != NULL;
        /* Opened before the first menu arrived: ask for one now so the
           window is not an empty frame until F1 happens to be pressed. */
        if (g_act_open && !g_menu_count && net_state() == NET_UP)
            send_frame(MSG_GET_MENU, NULL, 0);
    }
    CheckMenuItem(GetMenu(frame), IDM_SHOWBAR, MF_BYCOMMAND
        | (g_act_open ? MF_CHECKED : MF_UNCHECKED));
    save_ini();
    if (!g_user_arranged)
        layout_default();
}

long FAR PASCAL _export FrameProc(HWND hwnd, UINT msg, UINT wParam,
                                  LONG lParam)
{
    char err[128];
    TEXTMETRIC tm;
    HDC hdc;
    CLIENTCREATESTRUCT ccs;

    switch (msg) {
    case WM_CREATE:
        g_frame = hwnd;
        /* Colours before anything can paint: the background brush lives
           with the theme, and WM_CTLCOLOR hands it out. */
        theme_apply(g_theme);
        CheckMenuItem(GetMenu(hwnd), IDM_SHOWBAR, MF_BYCOMMAND
            | (g_act_open ? MF_CHECKED : MF_UNCHECKED));
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

        /* The MDI client owns the documents. It wants the Window menu
           by handle so it can append the child list to it, and the id
           it should start numbering those entries from. */
        ccs.hWindowMenu  = GetSubMenu(GetMenu(hwnd), WINDOW_MENU_POS);
        ccs.idFirstChild = IDM_FIRSTCHILD;
        g_mdi = CreateWindow("MDICLIENT", NULL,
                             WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
                             0, 0, 10, 10, hwnd, (HMENU)1,
                             (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE),
                             (LPSTR)&ccs);
        if (!g_mdi)
            return -1;
        launch_create(hwnd);
        g_conv = conv_create(hwnd);
        /* The picture window is part of the default desk, empty or not:
           an adventure fills it, and until then it says what it is.
           So are the Music controls, tucked in their corner - and the
           conversation takes the keyboard back from whatever opened
           last. */
        pic_open();
        mus_open_wnd();
        if (g_act_open)
            act_open_wnd();
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
        switch (wParam) {
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
            HINSTANCE inst = (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE);
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

        case IDM_SHOWBAR:
            act_toggle(hwnd);
            return 0;

        case IDM_THEME_PAPER:
        case IDM_THEME_SCREEN:
            theme_apply(wParam == IDM_THEME_SCREEN ? THEME_SCREEN
                                                   : THEME_PAPER);
            save_ini();
            return 0;

        case IDM_CLOSEPAPER: {
            int i;
            for (i = 0; i < MAX_PAPER; i++)
                if (g_paper_wnd[i])
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_paper_wnd[i], 0L);
            return 0;
        }

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
                act_toggle(hwnd);
                break;
            case 4:
                /* The Music window floats over the desk rather than
                   claiming a column - it is controls, not a document. */
                if (g_mus_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_mus_wnd, 0L);
                else
                    mus_open_wnd();
                break;
            case 5:
                if (g_chr_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_chr_wnd, 0L);
                else
                    sheet_open(CHR_CLASS, &g_chr_wnd, 80, 30, 260, 230);
                break;
            case 6:
                if (g_inv_wnd)
                    SendMessage(g_mdi, WM_MDIDESTROY,
                                (WPARAM)g_inv_wnd, 0L);
                else
                    sheet_open(INV_CLASS, &g_inv_wnd, 130, 70, 220, 190);
                break;
            }
            if (!g_user_arranged)
                layout_default();
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
        /* Children are destroyed after this handler; without the flag
           the Actions window would read its own teardown as the user
           closing it and write that choice to the INI. */
        g_quitting = 1;
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
    g_act_open = GetPrivateProfileInt("Display", "Actions", 0, g_ini)
        ? 1 : 0;
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
    WritePrivateProfileString("Display", "Actions",
                              g_act_open ? "1" : "0", g_ini);
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
        wc.style = CS_HREDRAW | CS_VREDRAW;
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

        /* Paper: a printed document, with no input box under it. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = PaperProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = GetStockObject(LTGRAY_BRUSH);
        wc.lpszMenuName = NULL;
        wc.lpszClassName = PAPER_CLASS;
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

        /* The Actions window: the server-fed menu as a document. */
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = ActProc;
        wc.hIcon = LoadIcon(NULL, IDI_APPLICATION);
        wc.hbrBackground = GetStockObject(LTGRAY_BRUSH);
        wc.lpszMenuName = NULL;
        wc.lpszClassName = ACT_CLASS;
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

    hwnd = CreateWindow(APP_CLASS, APP_TITLE, WS_OVERLAPPEDWINDOW,
                        CW_USEDEFAULT, CW_USEDEFAULT, 640, 440,
                        NULL, NULL, hInst, NULL);
    if (!hwnd)
        return 1;
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
