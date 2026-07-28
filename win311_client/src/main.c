/*
 * LLM64 for Windows - frame, windows and frame dispatch
 *
 * A 16-bit Windows 3.x client for the LLM64 proxy. It is an MDI
 * application: one frame window with the menu bar, the status strip and
 * the socket, and document windows inside it. Today there is one kind
 * of document - the conversation, an owner-drawn transcript pane over a
 * one-line input box - and the picture viewer, jukebox and conversation
 * manager join it as further children rather than as loose top-level
 * windows.
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
 * Deliberately absent, and scheduled: images, MIDI, printing, the
 * conversation manager and the settings dialog. See
 * win311_client/README.md.
 */

#include <windows.h>
#include <string.h>
#include <stdlib.h>
#include "wire.h"
#include "net.h"
#include "scroll.h"

#define APP_CLASS   "LLM64Main"
#define CONV_CLASS  "LLM64Conv"
#define PANE_CLASS  "LLM64Pane"
#define APP_TITLE   "LLM64"
#define INI_FILE    "LLM64.INI"

#define IDM_CONNECT     101
#define IDM_DISCONNECT  102
#define IDM_NEWCONV     103
#define IDM_EXIT        104
#define IDM_PING        105
#define IDM_CANCEL      106
#define IDM_ABOUT       107
#define IDM_CASCADE     108
#define IDM_TILE        109
#define IDM_ARRANGE     110
#define IDM_NEWWINDOW   111

/* Where the MDI client starts numbering its document windows on the
   Window menu. It has to sit above every command id above, because the
   frame routes anything at or over it straight to DefFrameProc. */
#define IDM_FIRSTCHILD  200

/* Position of the &Window popup in the menu bar: File, Link, Window. */
#define WINDOW_MENU_POS 2

#define ID_PANE     1000
#define ID_INPUT    1001

/* The transcript. Unwrapped logical lines in far blocks off the global
   heap, wrapped at paint time - see scroll.h. All that is left here is
   where we are looking at it from. */
static Scrollback g_sb;
static long       g_top = 0;    /* first visible display row */
static int        g_follow = 1; /* stick to the bottom as text arrives */

static HWND     g_frame;    /* the one top-level window */
static HWND     g_mdi;      /* MDICLIENT, between the frame and its docs */
static HWND     g_conv;     /* the conversation document */
static HWND     g_pane, g_input;    /* inside the conversation */
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

/* Pepto's C64 palette, the same table the proxy converts images with
   (llm64_proxy/src/imaging.py). Index 0 is black and never used as a
   text colour. */
static const COLORREF g_pal[16] = {
    RGB(0x00,0x00,0x00), RGB(0xFF,0xFF,0xFF), RGB(0x68,0x37,0x2B),
    RGB(0x70,0xA4,0xB2), RGB(0x6F,0x3D,0x86), RGB(0x58,0x8D,0x43),
    RGB(0x35,0x28,0x79), RGB(0xB8,0xC7,0x6F), RGB(0x6F,0x4F,0x25),
    RGB(0x43,0x39,0x00), RGB(0x9A,0x67,0x59), RGB(0x44,0x44,0x44),
    RGB(0x6C,0x6C,0x6C), RGB(0x9A,0xD2,0x84), RGB(0x6C,0x5E,0xB5),
    RGB(0x95,0x95,0x95)
};

/* ---------------------------------------------------------------- */
/* Transcript                                                        */
/* ---------------------------------------------------------------- */

static int pane_rows(void)
{
    RECT rc;
    if (!g_pane)
        return 1;
    GetClientRect(g_pane, &rc);
    return (rc.bottom / g_ch) > 0 ? (int)(rc.bottom / g_ch) : 1;
}

static int pane_cols(void)
{
    RECT rc;
    int c;
    if (!g_pane)
        return 40;
    GetClientRect(g_pane, &rc);
    c = (int)((rc.right - 4) / g_cw);
    if (c < 10) c = 10;
    return c;
}

/* Total rows, clamped to what a 16-bit scroll bar can express. Only a
   pathologically narrow pane over a full transcript can reach this. */
static long pane_total(void)
{
    unsigned long n = sb_rows(&g_sb);
    return n > 32000UL ? 32000L : (long)n;
}

static long pane_max_top(void)
{
    long max = pane_total() - pane_rows();
    return max < 0 ? 0 : max;
}

static void pane_sync_scroll(void)
{
    long max = pane_max_top();

    if (g_top > max) g_top = max;
    if (g_top < 0) g_top = 0;
    if (!g_pane)
        return;
    SetScrollRange(g_pane, SB_VERT, 0, (int)max, FALSE);
    SetScrollPos(g_pane, SB_VERT, (int)g_top, TRUE);
}

static void pane_bottom(void)
{
    g_top = pane_max_top();
    g_follow = 1;
    pane_sync_scroll();
}

/* Every write to the transcript ends the same way: pin to the bottom
   and repaint. The transcript itself is independent of any window, so
   text arriving while the document is closed is kept, not dropped - it
   is simply there when a window is opened on it again. */
static void pane_touch(void)
{
    pane_bottom();
    if (g_pane)
        InvalidateRect(g_pane, NULL, TRUE);
}

static void say(unsigned char color, const char *s)
{
    sb_say(&g_sb, color, s);
    pane_touch();
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
static void paint_row(HDC hdc, int y, const SbRow *r)
{
    unsigned i, run_start = 0;
    int x = 2, n;
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

static void pane_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc;
    SbView v;
    SbRow  r;
    int row, rows;
    HBRUSH bg;

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);
    bg = CreateSolidBrush(RGB(0, 0, 0));
    FillRect(hdc, &rc, bg);
    DeleteObject(bg);

    SetBkMode(hdc, TRANSPARENT);
    rows = pane_rows();
    if (sb_view(&g_sb, (unsigned long)g_top, &v)) {
        for (row = 0; row < rows && sb_view_next(&v, &r); row++)
            paint_row(hdc, row * g_ch, &r);
    }
    EndPaint(hwnd, &ps);
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
            sb_color(&g_sb, 13);
            sb_puts(&g_sb, (const char *)(p + 1));
            pane_touch();
        }
        break;

    case MSG_CHAT_DONE:
        sb_newline(&g_sb);
        sb_newline(&g_sb);
        pane_touch();
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

    switch (msg) {
    case WM_PAINT:
        pane_paint(hwnd);
        return 0;

    case WM_VSCROLL:
        rows = pane_rows();
        switch (wParam) {
        case SB_LINEUP:   g_top--; break;
        case SB_LINEDOWN: g_top++; break;
        case SB_PAGEUP:   g_top -= rows; break;
        case SB_PAGEDOWN: g_top += rows; break;
        case SB_THUMBPOSITION:
        case SB_THUMBTRACK: g_top = LOWORD(lParam); break;
        default: return 0;
        }
        pane_sync_scroll();
        /* Scrolling back to the bottom re-arms the follow: a reader who
           has paged up stays where they are while a reply streams in. */
        g_follow = (g_top >= pane_max_top());
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;

    case WM_SIZE:
        /* The whole point of the far-block transcript: a resize re-flows
           what is already on screen, because nothing was ever stored
           wrapped in the first place. */
        sb_width(&g_sb, (unsigned)pane_cols());
        if (g_follow)
            pane_bottom();
        else
            pane_sync_scroll();
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
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
        SendMessage(g_pane, WM_VSCROLL,
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
    MoveWindow(g_pane, 0, 0, rc.right, paneh, TRUE);
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
        g_pane = CreateWindow(PANE_CLASS, NULL,
                              WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_BORDER,
                              0, 0, 10, 10, hwnd, (HMENU)ID_PANE, inst, NULL);
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
        g_pane = NULL;
        g_input = NULL;
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
        if (!sb_init(&g_sb)) {
            MessageBox(hwnd, "Not enough memory for the transcript.",
                       APP_TITLE, MB_OK | MB_ICONSTOP);
            return -1;
        }
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
                             sb_clear(&g_sb);
                             pane_touch(); return 0;
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

        case IDM_NEWWINDOW:
            /* Closing the last document leaves an empty workspace, as
               it should in an MDI app - this is the way back. The
               transcript outlived the window, so it reappears with it. */
            if (g_conv)
                SendMessage(g_mdi, WM_MDIACTIVATE, (WPARAM)g_conv, 0L);
            else
                g_conv = conv_create(hwnd);
            return 0;

        case IDM_CASCADE: SendMessage(g_mdi, WM_MDICASCADE, 0, 0L); return 0;
        case IDM_TILE:    SendMessage(g_mdi, WM_MDITILE, 0, 0L); return 0;
        case IDM_ARRANGE: SendMessage(g_mdi, WM_MDIICONARRANGE, 0, 0L); return 0;
        }
        /* Anything else is either a document window being picked off the
           Window menu or a control notification: both belong to
           DefFrameProc, and swallowing them is how an MDI app quietly
           loses its Window menu. */
        break;

    case WM_DESTROY:
        net_shutdown();
        if (g_font_bold && g_font_bold != g_font)
            DeleteObject(g_font_bold);
        sb_free(&g_sb);
        PostQuitMessage(0);
        return 0;
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
    GetPrivateProfileString("Server", "Host", "127.0.0.1",
                            g_host, sizeof(g_host), g_ini);
    g_port = GetPrivateProfileInt("Server", "Port", 6400, g_ini);
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

        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = PaneProc;
        wc.hIcon = NULL;
        wc.hbrBackground = NULL;
        wc.lpszMenuName = NULL;
        wc.lpszClassName = PANE_CLASS;
        if (!RegisterClass(&wc))
            return 1;
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
