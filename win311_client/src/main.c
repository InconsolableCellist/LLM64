/*
 * LLM64 for Windows - Phase 0 spike
 *
 * A 16-bit Windows 3.x client for the LLM64 proxy: main window with a
 * menu bar, an owner-drawn transcript pane, a one-line input box and a
 * status strip. It connects over Winsock, PINGs, and holds a chat.
 *
 * What this spike is proving (docs/16 section 10):
 *   - Open Watcom builds a working NE binary for Windows 3.x
 *   - it runs under Wine's 16-bit subsystem
 *   - WSAAsyncSelect drives the protocol without ever blocking
 *   - the +0x20 length bias round-trips against the real proxy
 *   - the in-band colour markers render as colour
 *
 * Deliberately absent, and scheduled: images, MIDI, printing, the
 * conversation manager, the settings dialog, and a scrollback that is
 * not a fixed array. See win311_client/README.md.
 */

#include <windows.h>
#include <string.h>
#include <stdlib.h>
#include "wire.h"
#include "net.h"

#define APP_CLASS   "LLM64Main"
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

#define ID_PANE     1000
#define ID_INPUT    1001

/* Scrollback. A fixed array for now: the Win16 default data segment is
   64 KB and this is 200 x 160 = 32 KB of it, which is the ceiling this
   approach can reach. Phase 2 replaces it with a list of GlobalAlloc'd
   blocks - see README. */
#define MAX_LINES   200
#define MAX_COLS    160

typedef struct {
    char          text[MAX_COLS + 1];
    unsigned char len;
    unsigned char color;     /* C64 colour index the line starts in */
} Line;

static Line          g_lines[MAX_LINES];
static int           g_nlines = 0;      /* lines in use */
static int           g_top = 0;         /* first visible line */
static unsigned char g_cur_color = 13;  /* light green: assistant */
static int           g_open_line = 0;   /* line being appended to */

static HWND     g_main, g_pane, g_input;
static HFONT    g_font;
static int      g_cw = 8, g_ch = 16;    /* character cell */
static FARPROC  g_old_edit_proc;
static char     g_status[128] = "Not connected.";
static char     g_host[64];
static unsigned g_port;

static unsigned char g_rxbuf[WIRE_MAX_PAYLOAD];
static WireRx        g_rx;
static unsigned char g_frame[WIRE_MAX_PAYLOAD + 8];

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
    if (c > MAX_COLS) c = MAX_COLS;
    return c;
}

static void pane_sync_scroll(void)
{
    int rows = pane_rows();
    int max = g_nlines - rows;
    if (max < 0) max = 0;
    if (g_top > max) g_top = max;
    if (g_top < 0) g_top = 0;
    SetScrollRange(g_pane, SB_VERT, 0, max, FALSE);
    SetScrollPos(g_pane, SB_VERT, g_top, TRUE);
}

static void pane_bottom(void)
{
    int rows = pane_rows();
    g_top = g_nlines - rows;
    if (g_top < 0) g_top = 0;
    pane_sync_scroll();
}

/* Drop the oldest line when the array is full. A memmove of 32 KB is
   not free, but it happens once per line at the very end of a long
   session, and it keeps the indices trivially correct. */
static void scroll_out(void)
{
    memmove(&g_lines[0], &g_lines[1], sizeof(Line) * (MAX_LINES - 1));
    g_nlines = MAX_LINES - 1;
    g_open_line = g_nlines;
    memset(&g_lines[g_open_line], 0, sizeof(Line));
    g_lines[g_open_line].color = g_cur_color;
}

static void line_new(void)
{
    if (g_nlines >= MAX_LINES)
        scroll_out();
    else
        g_open_line = g_nlines++;
    memset(&g_lines[g_open_line], 0, sizeof(Line));
    g_lines[g_open_line].color = g_cur_color;
}

static void append_char(char c)
{
    Line *ln;
    int cols = pane_cols();
    int brk, i, n;

    if (g_nlines == 0)
        line_new();
    ln = &g_lines[g_open_line];

    if (c == '\n') {
        line_new();
        return;
    }
    if (ln->len >= (unsigned char)cols) {
        /* Word wrap: carry the last unfinished word to the next line
           rather than splitting it. */
        brk = -1;
        for (i = ln->len - 1; i > 0 && i > ln->len - 24; i--) {
            if (ln->text[i] == ' ') { brk = i; break; }
        }
        if (brk > 0) {
            char carry[32];
            n = ln->len - brk - 1;
            if (n > 30) n = 30;
            memcpy(carry, ln->text + brk + 1, n);
            ln->len = (unsigned char)brk;
            ln->text[brk] = '\0';
            line_new();
            ln = &g_lines[g_open_line];
            memcpy(ln->text, carry, n);
            ln->len = (unsigned char)n;
            ln->text[n] = '\0';
        } else {
            line_new();
            ln = &g_lines[g_open_line];
        }
    }
    ln->text[ln->len++] = c;
    ln->text[ln->len] = '\0';
}

/* Append proxy text, markers and all. The markers stay in the buffer
   because they are what the painter splits colour runs on. */
static void append_text(const char *s)
{
    while (*s)
        append_char(*s++);
}

/* A line's base colour is stamped when the line is created, so changing
   the current colour has to restamp a line that has not been written to
   yet - otherwise the first chunk of a reply inherits the colour of
   whatever came before it. */
static void set_color(unsigned char color)
{
    g_cur_color = color;
    if (g_nlines > 0 && g_lines[g_open_line].len == 0)
        g_lines[g_open_line].color = color;
}

static void say(unsigned char color, const char *s)
{
    g_cur_color = color;
    line_new();
    append_text(s);
    line_new();
    pane_bottom();
    InvalidateRect(g_pane, NULL, TRUE);
}

static void set_status(const char *s)
{
    lstrcpyn(g_status, s, sizeof(g_status) - 1);
    if (g_main)
        InvalidateRect(g_main, NULL, FALSE);
}

/* ---------------------------------------------------------------- */
/* Painting                                                          */
/* ---------------------------------------------------------------- */

static void paint_line(HDC hdc, int y, const Line *ln)
{
    int i, run_start = 0, x = 2;
    unsigned char color = ln->color;
    int bold = 0;
    char buf[MAX_COLS + 1];
    int n;

    for (i = 0; i <= (int)ln->len; i++) {
        unsigned char c = (unsigned char)ln->text[i];
        int is_marker = (i < (int)ln->len)
            && (c == MARK_CLOSE || c == MARK_BOLD_ON || c == MARK_BOLD_OFF
                || (c >= MARK_COLOR_BASE + 1 && c <= MARK_COLOR_BASE + 14));

        if (i == (int)ln->len || is_marker) {
            n = i - run_start;
            if (n > 0) {
                memcpy(buf, ln->text + run_start, n);
                buf[n] = '\0';
                SetTextColor(hdc, g_pal[color & 0x0F]);
                SelectObject(hdc, g_font);
                TextOut(hdc, x, y, buf, n);
                x += n * g_cw;
            }
            if (is_marker) {
                if (c == MARK_CLOSE)         color = ln->color;
                else if (c == MARK_BOLD_ON)  bold = 1;
                else if (c == MARK_BOLD_OFF) bold = 0;
                else                         color = (unsigned char)(c & 0x0F);
            }
            run_start = i + 1;
        }
    }
    (void)bold;   /* a bold face lands with the font work in Phase 1 */
}

static void pane_paint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC hdc;
    RECT rc;
    int row, rows, idx, y;
    HBRUSH bg;

    hdc = BeginPaint(hwnd, &ps);
    GetClientRect(hwnd, &rc);
    bg = CreateSolidBrush(RGB(0, 0, 0));
    FillRect(hdc, &rc, bg);
    DeleteObject(bg);

    SetBkMode(hdc, TRANSPARENT);
    rows = pane_rows();
    for (row = 0; row < rows; row++) {
        idx = g_top + row;
        if (idx >= g_nlines)
            break;
        y = row * g_ch;
        paint_line(hdc, y, &g_lines[idx]);
    }
    EndPaint(hwnd, &ps);
}

/* ---------------------------------------------------------------- */
/* Protocol                                                          */
/* ---------------------------------------------------------------- */

static void send_frame(unsigned char type, const unsigned char *payload,
                       unsigned len)
{
    unsigned n = wire_frame(g_frame, type, payload, len);
    if (!net_send(g_frame, n))
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
            set_color(13);
            append_text((const char *)(p + 1));
            pane_bottom();
            InvalidateRect(g_pane, NULL, TRUE);
        }
        break;

    case MSG_CHAT_DONE:
        line_new();
        line_new();
        pane_bottom();
        InvalidateRect(g_pane, NULL, TRUE);
        set_status("Ready.");
        EnableWindow(g_input, TRUE);
        SetFocus(g_input);
        break;

    case MSG_CHAT_ERROR:
        say(2, (const char *)p);
        set_status("Error.");
        EnableWindow(g_input, TRUE);
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

    n = GetWindowText(g_input, text, sizeof(text) - 1);
    if (n <= 0)
        return;
    text[n] = '\0';
    SetWindowText(g_input, "");

    if (net_state() != NET_UP) {
        say(2, "Not connected. Use File > Connect.");
        return;
    }
    g_cur_color = 1;
    say(1, text);
    send_text_frame(MSG_CHAT_REQUEST, text);
    set_status("Waiting for the model...");
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
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;

    case WM_SIZE:
        pane_sync_scroll();
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

static void layout(HWND hwnd)
{
    RECT rc;
    int inputh = g_ch + 8;
    int statush = g_ch + 6;
    int paneh;

    GetClientRect(hwnd, &rc);
    paneh = rc.bottom - inputh - statush;
    if (paneh < g_ch) paneh = g_ch;
    MoveWindow(g_pane, 0, 0, rc.right, paneh, TRUE);
    MoveWindow(g_input, 0, paneh, rc.right, inputh, TRUE);
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
    say(1, "LLM64 for Windows - Phase 0 spike");
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

long FAR PASCAL _export MainProc(HWND hwnd, UINT msg, UINT wParam,
                                 LONG lParam)
{
    char err[128];
    TEXTMETRIC tm;
    HDC hdc;

    switch (msg) {
    case WM_CREATE:
        g_main = hwnd;
        g_font = GetStockObject(SYSTEM_FIXED_FONT);
        hdc = GetDC(hwnd);
        SelectObject(hdc, g_font);
        GetTextMetrics(hdc, &tm);
        g_cw = tm.tmAveCharWidth;
        g_ch = tm.tmHeight;
        ReleaseDC(hwnd, hdc);

        g_pane = CreateWindow(PANE_CLASS, NULL,
                              WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_BORDER,
                              0, 0, 10, 10, hwnd, (HMENU)ID_PANE,
                              (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE),
                              NULL);
        g_input = CreateWindow("EDIT", "",
                               WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL,
                               0, 0, 10, 10, hwnd, (HMENU)ID_INPUT,
                               (HINSTANCE)GetWindowWord(hwnd, GWW_HINSTANCE),
                               NULL);
        SendMessage(g_input, WM_SETFONT, (WPARAM)g_font, 0L);
        g_old_edit_proc = (FARPROC)GetWindowLong(g_input, GWL_WNDPROC);
        SetWindowLong(g_input, GWL_WNDPROC, (LONG)EditProc);

        return 0;

    case WM_SIZE:
        layout(hwnd);
        /* Nothing may be written to the transcript before the pane has
           its real size: lines are wrapped as they are appended, and at
           WM_CREATE time the pane is still the 10x10 placeholder that
           CreateWindow was given. */
        if (!g_started) {
            g_started = 1;
            start_session(hwnd);
        }
        return 0;

    case WM_SETFOCUS:
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
                             g_nlines = 0; g_top = 0;
                             InvalidateRect(g_pane, NULL, TRUE); return 0;
        case IDM_CANCEL:     send_frame(MSG_CANCEL_REQUEST, NULL, 0);
                             EnableWindow(g_input, TRUE); return 0;
        case IDM_ABOUT:
            MessageBox(hwnd,
                       "LLM64 for Windows\n\n"
                       "A Windows 3.1 client for the LLM64 proxy.\n"
                       "Phase 0 spike.",
                       "About LLM64", MB_OK | MB_ICONINFORMATION);
            return 0;
        case IDM_EXIT:
            PostMessage(hwnd, WM_CLOSE, 0, 0L);
            return 0;
        }
        return 0;

    case WM_DESTROY:
        net_shutdown();
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

static void load_ini(void)
{
    GetPrivateProfileString("Server", "Host", "127.0.0.1",
                            g_host, sizeof(g_host), INI_FILE);
    g_port = GetPrivateProfileInt("Server", "Port", 6400, INI_FILE);
}

int PASCAL WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR cmdline, int show)
{
    WNDCLASS wc;
    HWND hwnd;
    MSG msg;
    char *p;

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
        wc.lpfnWndProc = MainProc;
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
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    return msg.wParam;
}
