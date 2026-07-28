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
#include <string.h>
#include <stdlib.h>
#include "wire.h"
#include "net.h"
#include "scroll.h"
#include "resource.h"

#define APP_CLASS   "LLM64Main"
#define CONV_CLASS  "LLM64Conv"
#define PAPER_CLASS "LLM64Paper"
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
static HFONT    g_font, g_font_bold;
static int      g_cw = 8, g_ch = 16;    /* character cell */
static FARPROC  g_old_edit_proc;
static char     g_status[128] = "Not connected.";
static char     g_host[64];
static unsigned g_port;
static char     g_ini[160];     /* full path to LLM64.INI */

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

/* Pepto's C64 palette, the same table the proxy converts images with
   (llm64_proxy/src/imaging.py). Index 0 is black and never used as a
   text colour. */
static const COLORREF g_pal_screen[16] = {
    RGB(0x00,0x00,0x00), RGB(0xFF,0xFF,0xFF), RGB(0x68,0x37,0x2B),
    RGB(0x70,0xA4,0xB2), RGB(0x6F,0x3D,0x86), RGB(0x58,0x8D,0x43),
    RGB(0x35,0x28,0x79), RGB(0xB8,0xC7,0x6F), RGB(0x6F,0x4F,0x25),
    RGB(0x43,0x39,0x00), RGB(0x9A,0x67,0x59), RGB(0x44,0x44,0x44),
    RGB(0x6C,0x6C,0x6C), RGB(0x9A,0xD2,0x84), RGB(0x6C,0x5E,0xB5),
    RGB(0x95,0x95,0x95)
};

/* The same fourteen marker slots as inks on white paper. Not the C64
   colours dimmed: half of them are unreadable on white at any
   brightness - yellow and light green worst of all, and light green is
   the colour every assistant reply arrives in. So each slot keeps its
   *hue* and takes a value that reads as ink. Index 1, the default text
   colour, becomes black; index 13 becomes a dark green, which is what
   makes a reply legible rather than merely present. */
static const COLORREF g_pal_paper[16] = {
    RGB(0x00,0x00,0x00), RGB(0x00,0x00,0x00), RGB(0xB0,0x14,0x14),
    RGB(0x00,0x70,0x80), RGB(0x80,0x20,0x90), RGB(0x1C,0x70,0x20),
    RGB(0x18,0x28,0xA8), RGB(0x80,0x70,0x00), RGB(0xB0,0x5A,0x00),
    RGB(0x70,0x44,0x10), RGB(0xC0,0x40,0x38), RGB(0x50,0x50,0x50),
    RGB(0x68,0x68,0x68), RGB(0x00,0x64,0x1E), RGB(0x40,0x40,0xB0),
    RGB(0x78,0x78,0x78)
};

#define THEME_PAPER   0
#define THEME_SCREEN  1

static int       g_theme = THEME_PAPER;
static const COLORREF *g_pal = g_pal_paper;
static COLORREF  g_bg = RGB(0xFF,0xFF,0xFF);
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
        InvalidateRect(v->pane, NULL, TRUE);
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

/* Draw one display row. The markers are still in the text - they are
   what the runs are split on, and the row arrives already knowing the
   colour and weight in force at its first cell, which is what makes a
   span that survives a wrap render the same on both rows. */
static void paint_row(HDC hdc, int x0, int y, const SbRow *r)
{
    unsigned i, run_start = 0;
    int x = x0, n;
    unsigned char color = r->color;
    unsigned char bold = r->bold;

    /* i == r->len closes the last run, and at that point there is no
       byte to read: a row can end flush against the end of an arena
       block, and in protected mode reading one past it is a fault, not
       a stray byte. The Phase 0 version got away with this because its
       lines were NUL-terminated arrays. */
    for (i = 0; i <= r->len; i++) {
        unsigned char c = (i < r->len) ? (unsigned char)r->text[i] : 0;
        int is_marker = (i < r->len) && sb_is_marker(c);

        if (i == r->len || is_marker) {
            n = (int)(i - run_start);
            if (n > 0) {
                SetTextColor(hdc, g_pal[color & 0x0F]);
                SelectObject(hdc, bold ? g_font_bold : g_font);
                TextOut(hdc, x, y, (LPSTR)(r->text + run_start), n);
                x += n * g_cw;
            }
            if (is_marker) {
                if (c == MARK_CLOSE)         color = r->base;
                else if (c == MARK_BOLD_ON)  bold = 1;
                else if (c == MARK_BOLD_OFF) bold = 0;
                else                         color = (unsigned char)(c & 0x0F);
            }
            run_start = i + 1;
        }
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
    HBRUSH bg;
    View *v = pane_view(hwnd);

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);
    bg = CreateSolidBrush(g_bg);
    FillRect(hdc, &rc, bg);
    DeleteObject(bg);

    if (!v || !v->live) {
        EndPaint(hwnd, &ps);
        return;
    }

    SetBkMode(hdc, TRANSPARENT);
    rows = view_rows(v);
    if (sb_view(&v->sb, (unsigned long)v->top, &it)) {
        for (row = 0; row < rows && sb_view_next(&it, &r); row++)
            paint_row(hdc, v->margin, row * g_ch, &r);
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
        set_status("Proxy answered the ping - link is good.");
        break;

    case MSG_HINT:
        /* [flags][pics][chrome\0] - the proxy-composed right-hand
           status text. Phase 1 gives it its own half of the strip. */
        if (len > 2)
            set_status((const char *)(p + 2));
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

/* The input box is a stock EDIT control; subclassing is how it learns
   that Return means send. */
long FAR PASCAL _export EditProc(HWND hwnd, UINT msg, UINT wParam,
                                 LONG lParam)
{
    if (msg == WM_CHAR && wParam == VK_RETURN) {
        send_input();
        return 0;
    }
    if (msg == WM_KEYDOWN && (wParam == VK_PRIOR || wParam == VK_NEXT)) {
        SendMessage(g_conv_view.pane, WM_VSCROLL,
                    wParam == VK_PRIOR ? SB_PAGEUP : SB_PAGEDOWN, 0L);
        return 0;
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

/* The frame gives everything except the status strip to the MDI client,
   which is what actually owns the document windows. */
static void frame_layout(HWND hwnd)
{
    RECT rc;
    int statush = g_ch + 6;
    int h;

    if (!g_mdi)
        return;
    GetClientRect(hwnd, &rc);
    h = rc.bottom - statush;
    if (h < g_ch) h = g_ch;
    MoveWindow(g_mdi, 0, 0, rc.right, h, TRUE);
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
        conv_layout(hwnd);
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
    SetBkMode(hdc, TRANSPARENT);
    SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SetTextColor(hdc, RGB(0, 0, 0));
    TextOut(hdc, 4, sr.top + 3, g_status, lstrlen(g_status));
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

/* Server settings. The one dialog the client cannot do without on a real
   machine: there is no command line there, so without this the only way
   to change the address is to rebuild the disk the program came on. */
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

/* Open the conversation document. One today; the same call is how a
   picture viewer or a jukebox will arrive. */
static HWND conv_create(HWND frame)
{
    MDICREATESTRUCT mcs;

    mcs.szClass = CONV_CLASS;
    mcs.szTitle = "Conversation";
    mcs.hOwner  = (HINSTANCE)GetWindowWord(frame, GWW_HINSTANCE);
    mcs.x       = CW_USEDEFAULT;
    mcs.y       = CW_USEDEFAULT;
    mcs.cx      = CW_USEDEFAULT;
    mcs.cy      = CW_USEDEFAULT;
    /* Maximized: with a single document the workspace border is all
       cost and no information. Restoring it is a click away, and that
       is where a second window starts making sense. */
    mcs.style   = WS_MAXIMIZE;
    mcs.lParam  = 0;

    return (HWND)(WORD)SendMessage(g_mdi, WM_MDICREATE, 0,
                                   (LONG)(LPMDICREATESTRUCT)&mcs);
}

long FAR PASCAL _export FrameProc(HWND hwnd, UINT msg, UINT wParam,
                                  LONG lParam)
{
    char err[128];
    TEXTMETRIC tm;
    LOGFONT lf;
    HDC hdc;
    CLIENTCREATESTRUCT ccs;

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

        /* A bold face for the proxy's bold markers. It has to keep the
           cell width or the pane stops being a grid, so ask for the
           measured width explicitly and check we got it. */
        g_font_bold = g_font;
        if (GetObject(g_font, sizeof(lf), (LPSTR)&lf)) {
            HFONT f;
            lf.lfWeight = FW_BOLD;
            lf.lfWidth  = g_cw;
            f = CreateFontIndirect(&lf);
            if (f) {
                SelectObject(hdc, f);
                GetTextMetrics(hdc, &tm);
                if (tm.tmAveCharWidth == g_cw)
                    g_font_bold = f;
                else
                    DeleteObject(f);
                SelectObject(hdc, g_font);
                GetTextMetrics(hdc, &tm);
            }
        }
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
        g_conv = conv_create(hwnd);
        return 0;

    case WM_SIZE:
        frame_layout(hwnd);
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
                send_frame(MSG_PING, NULL, 0);
            } else {
                set_status(err);
                say(2, err);
            }
        } else if (event == NET_EV_READ) {
            pump_socket();
        } else if (event == NET_EV_CLOSE) {
            print_abort();
            set_status(err);
            say(2, "Disconnected.");
        }
        return 0;
    }

    case WM_COMMAND:
        switch (wParam) {
        case IDM_CONNECT:    do_connect(); return 0;
        case IDM_DISCONNECT: net_disconnect();
                             set_status("Disconnected."); return 0;
        case IDM_PING:       send_frame(MSG_PING, NULL, 0);
                             set_status("Ping sent."); return 0;
        case IDM_NEWCONV:    send_frame(MSG_NEW_CONVERSATION, NULL, 0);
                             sb_clear(&g_conv_view.sb);
                             view_touch(&g_conv_view); return 0;
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

        case IDM_SERVER: {
            HINSTANCE inst = (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE);
            /* MakeProcInstance is not optional in Win16: the dialog is
               called back through a thunk that reloads DS for this
               instance. */
            FARPROC fn = MakeProcInstance((FARPROC)ServerDlgProc, inst);
            int r = DialogBox(inst, "LLM64SERVER", hwnd, (DLGPROC)fn);
            FreeProcInstance(fn);
            if (r == 2) {
                net_disconnect();
                do_connect();
            } else if (r == 1) {
                char msg2[160];
                wsprintf(msg2, "Server set to %s:%u - connect when ready.",
                         (LPSTR)g_host, g_port);
                set_status(msg2);
            }
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
        /* Anything else is either a document window being picked off the
           Window menu or a control notification: both belong to
           DefFrameProc, and swallowing them is how an MDI app quietly
           loses its Window menu. */
        break;

    case WM_DESTROY: {
        int i;
        net_shutdown();
        if (g_font_bold && g_font_bold != g_font)
            DeleteObject(g_font_bold);
        if (g_bg_brush)
            DeleteObject(g_bg_brush);
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
}

int PASCAL WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR cmdline, int show)
{
    WNDCLASS wc;
    HWND hwnd;
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

    while (GetMessage(&msg, NULL, 0, 0)) {
        /* Ctrl+F4, Ctrl+F6 and the rest of the MDI system accelerators
           are the document windows', and they have to be offered the
           message before the frame translates it. */
        if (!TranslateMDISysAccel(g_mdi, &msg)) {
            TranslateMessage(&msg);
            DispatchMessage(&msg);
        }
    }
    return msg.wParam;
}
