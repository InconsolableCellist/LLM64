/*
 * Tier 2 spike: a Windows 3.1 window frame, drawn by us
 *
 * The question this answers, and the only one: if we stop letting the OS
 * paint the non-client area and draw the 3.1 caption ourselves, does it
 * read as 1993? Everything here exists to be screenshotted next to
 * screenshots/win311_client.png and judged.
 *
 * So this is deliberately NOT integrated with main.c. It is a standalone
 * program with no proxy, no MDI and no menu bar, because the visual
 * question does not need any of them and a spike that touches the working
 * client cannot be thrown away. If the answer is yes, cap_paint() and
 * cap_hittest() lift across roughly whole.
 *
 * Every metric and colour in here was measured off the reference capture
 * rather than remembered:
 *
 *   caption      18 px tall, #000080 flat, no gradient
 *   below it     one 1 px black rule
 *   buttons      square, the caption's height, C0C0C0 with a hard bevel,
 *                separated from the navy by a 1 px black line
 *   sysmenu      a white bar with a black outline - not a minus sign
 *   minimise     a solid black down triangle
 *   maximise     a solid black up triangle (BOTH arrows when maximised,
 *                which is 3.1's restore glyph)
 *   title        centred, white, the System stock font
 *   NO close button. 3.1 has none, and this is the loudest single tell.
 *
 * Build:  make spike     ->  build/CAPSPIKE.EXE
 */

#include <windows.h>
#include <windowsx.h>   /* GET_X_LPARAM / GET_Y_LPARAM */
#include <string.h>     /* strstr, for the command line */

#define CAP_H       18      /* caption, measured */
#define BTN_W       18      /* measured: square, the caption height */
#define FRAME       4       /* sizing border: 1 black + 3 bevel */

/* A maximised 3.1 window has NO border - it is flush to the screen, which
   is why the reference capture's sysmenu box starts at x=0. Getting this
   wrong puts a grey margin around the caption and is the first thing that
   reads as not-quite-right. */
#define FRAME_W()   (g_maxed ? 0 : FRAME)
#define RULE        1       /* the black line under the caption */

/* The 3.1 system palette. Hardcoded on purpose: GetSysColor() on a
   modern machine returns 2026 values (COLOR_BTNFACE is #F0F0F0, not
   #C0C0C0), and SetSysColors is global and not ours to touch. Owning
   these is the whole point. */
#define C_FACE      RGB(0xC0, 0xC0, 0xC0)
#define C_HILIGHT   RGB(0xFF, 0xFF, 0xFF)
#define C_SHADOW    RGB(0x80, 0x80, 0x80)
#define C_FRAME     RGB(0x00, 0x00, 0x00)
#define C_ACTIVE    RGB(0x00, 0x00, 0x80)   /* measured: exactly #000080 */
#define C_INACTIVE  RGB(0xC0, 0xC0, 0xC0)
#define C_ACTTEXT   RGB(0xFF, 0xFF, 0xFF)
#define C_INACTTEXT RGB(0x00, 0x00, 0x00)
#define C_CLIENT    RGB(0xFF, 0xFF, 0xFF)

/* Hit codes for our own buttons. Anything the OS should handle (drag,
   resize) gets a real HT* code back instead and DefWindowProc does the
   work - we are replacing the painting, not the window management. */
#define HIT_NONE    0
#define HIT_SYS     1
#define HIT_MIN     2
#define HIT_MAX     3


static int  g_down = HIT_NONE;      /* button held, for the pressed bevel */
static int  g_maxed = 0;

/* ------------------------------------------------------------------ */

/* A 3.1 bevel: highlight on top and left, shadow on bottom and right.
   `out` raises the surface, !out sinks it, which is all a pressed button
   is. */
static void bevel(HDC hdc, const RECT *r, int out)
{
    HBRUSH hi = CreateSolidBrush(out ? C_HILIGHT : C_SHADOW);
    HBRUSH lo = CreateSolidBrush(out ? C_SHADOW : C_HILIGHT);
    RECT e;

    e = *r; e.bottom = e.top + 1;      FillRect(hdc, &e, hi);
    e = *r; e.right  = e.left + 1;     FillRect(hdc, &e, hi);
    e = *r; e.top    = e.bottom - 1;   FillRect(hdc, &e, lo);
    e = *r; e.left   = e.right - 1;    FillRect(hdc, &e, lo);

    DeleteObject(hi);
    DeleteObject(lo);
}

static void fill(HDC hdc, const RECT *r, COLORREF c)
{
    HBRUSH b = CreateSolidBrush(c);

    FillRect(hdc, r, b);
    DeleteObject(b);
}

/* A solid triangle, point up or down. 3.1's arrows are filled, not
   outlined, and they are wider than they are tall. */
static void triangle(HDC hdc, int cx, int cy, int up)
{
    POINT p[3];
    HBRUSH b = CreateSolidBrush(C_FRAME);
    HBRUSH ob;
    HPEN op;

    p[0].x = cx - 4; p[0].y = up ? cy + 2 : cy - 2;
    p[1].x = cx + 4; p[1].y = p[0].y;
    p[2].x = cx;     p[2].y = up ? cy - 3 : cy + 3;

    ob = SelectObject(hdc, b);
    op = SelectObject(hdc, GetStockObject(NULL_PEN));
    Polygon(hdc, p, 3);
    SelectObject(hdc, ob);
    SelectObject(hdc, op);
    DeleteObject(b);
}

/* Where the three buttons sit. One place, so paint and hit-test can
   never disagree - which is the usual way hand-drawn chrome goes subtly
   wrong. */
static void btn_rects(HWND hwnd, RECT *sys, RECT *mn, RECT *mx)
{
    RECT rc;
    int top = FRAME_W();
    int bot = FRAME_W() + CAP_H;

    GetClientRect(hwnd, &rc);

    sys->left = FRAME_W();      sys->right = FRAME_W() + BTN_W;
    mx->right = rc.right - FRAME_W();   mx->left  = mx->right - BTN_W;
    mn->right = mx->left;           mn->left  = mn->right - BTN_W;

    sys->top = mn->top = mx->top = top;
    sys->bottom = mn->bottom = mx->bottom = bot;
}

static void cap_paint(HWND hwnd, HDC hdc)
{
    RECT rc, r, sys, mn, mx;
    int active = (GetActiveWindow() == hwnd);
    char title[128];
    int n;
    HFONT of;

    GetClientRect(hwnd, &rc);

    /* The sizing border: one black line outside, a raised bevel inside.
       3.1's frame is thick because it is the resize grip. Skipped entirely
       when maximised, where there is no border to grip. */
    if (FRAME_W()) {
        HBRUSH b = CreateSolidBrush(C_FRAME);

        r = rc;
        fill(hdc, &r, C_FACE);
        FrameRect(hdc, &r, b);
        DeleteObject(b);
        r = rc;
        InflateRect(&r, -1, -1);
        bevel(hdc, &r, 1);
    }
    (void)n;

    /* Caption. */
    r.left   = FRAME_W();
    r.right  = rc.right - FRAME_W();
    r.top    = FRAME_W();
    r.bottom = FRAME_W() + CAP_H;
    fill(hdc, &r, active ? C_ACTIVE : C_INACTIVE);

    /* The 1 px black rule under it, full width inside the frame. */
    r.top = FRAME_W() + CAP_H;
    r.bottom = r.top + RULE;
    fill(hdc, &r, C_FRAME);

    btn_rects(hwnd, &sys, &mn, &mx);

    /* Title, centred - the single most recognisable 3.1 tell. Windows 95
       moved it left and every "retro" UI since has copied 95.

       Centred across the WHOLE caption, not across the gap between the
       button clusters. The gap is asymmetric - one button on the left,
       two on the right - so centring in it lands the title about 10 px
       right of where 3.1 puts it. That was visible the moment the two
       captions were stacked, and invisible before. */
    n = GetWindowText(hwnd, title, sizeof(title) - 1);
    title[n < 0 ? 0 : n] = '\0';
    r.left   = FRAME_W();
    r.right  = rc.right - FRAME_W();
    r.top    = FRAME_W();
    r.bottom = FRAME_W() + CAP_H;
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, active ? C_ACTTEXT : C_INACTTEXT);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    DrawText(hdc, title, -1, &r,
             DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    SelectObject(hdc, of);

    /* The buttons. Each is a raised C0C0C0 face with a black line facing
       the navy, so the cluster reads as separated from the caption. */
    {
        RECT *all[3];
        int i;

        all[0] = &sys; all[1] = &mn; all[2] = &mx;
        for (i = 0; i < 3; i++) {
            RECT b = *all[i];
            int pressed = (g_down == (i == 0 ? HIT_SYS
                                             : i == 1 ? HIT_MIN : HIT_MAX));

            fill(hdc, &b, C_FACE);
            bevel(hdc, &b, !pressed);
            if (pressed)
                OffsetRect(&b, 1, 1);

            if (i == 0) {
                /* Sysmenu: a white bar with a black outline. Not a minus
                   sign, and not the 95 icon that replaced it. */
                RECT g;
                int cx = (b.left + b.right) / 2;
                int cy = (b.top + b.bottom) / 2;
                HBRUSH k = CreateSolidBrush(C_FRAME);

                g.left = cx - 6; g.right = cx + 6;
                g.top  = cy - 3; g.bottom = cy + 3;
                fill(hdc, &g, C_HILIGHT);
                FrameRect(hdc, &g, k);
                DeleteObject(k);
            } else if (i == 1) {
                triangle(hdc, (b.left + b.right) / 2,
                         (b.top + b.bottom) / 2, 0);
            } else if (g_maxed) {
                /* Maximised: BOTH arrows. This is 3.1's restore glyph and
                   it is what the reference capture shows, because that
                   window was maximised. */
                triangle(hdc, (b.left + b.right) / 2,
                         (b.top + b.bottom) / 2 - 4, 1);
                triangle(hdc, (b.left + b.right) / 2,
                         (b.top + b.bottom) / 2 + 4, 0);
            } else {
                triangle(hdc, (b.left + b.right) / 2,
                         (b.top + b.bottom) / 2, 1);
            }
        }
        /* Separators, measured off the reference: one black column to the
           right of the sysmenu box, one between the two arrows, and none
           to the LEFT of the minimise button - there the navy runs
           straight into the button's white highlight. */
        r = sys; r.left = r.right; r.right = r.left + 1;
        fill(hdc, &r, C_FRAME);
        r = mx; r.right = r.left; r.left = r.right - 1;
        fill(hdc, &r, C_FRAME);
    }

    /* Client area, with a note so the screenshot explains itself. */
    r.left   = FRAME_W();
    r.right  = rc.right - FRAME_W();
    r.top    = FRAME_W() + CAP_H + RULE;
    r.bottom = rc.bottom - FRAME_W();
    fill(hdc, &r, C_CLIENT);
    SetTextColor(hdc, RGB(0, 0, 0));
    of = SelectObject(hdc, GetStockObject(SYSTEM_FIXED_FONT));
    InflateRect(&r, -8, -8);
    DrawText(hdc,
             "Tier 2 spike.\r\n\r\n"
             "This caption is drawn by the program, not by Windows:\r\n"
             "centred System-font title, sysmenu bar glyph, a down\r\n"
             "arrow and an up arrow, flat #000080, and no close\r\n"
             "button - because Windows 3.1 does not have one.\r\n\r\n"
             "Drag the caption. Drag any edge or corner to resize.\r\n"
             "The buttons work. The corners are square on Windows 11.",
             -1, &r, DT_LEFT | DT_TOP | DT_NOPREFIX);
    SelectObject(hdc, of);
}

/* Which of our own buttons is under a CLIENT-space point. */
static int cap_button_at(HWND hwnd, POINT pt)
{
    RECT sys, mn, mx;

    btn_rects(hwnd, &sys, &mn, &mx);
    if (PtInRect(&sys, pt)) return HIT_SYS;
    if (PtInRect(&mn, pt))  return HIT_MIN;
    if (PtInRect(&mx, pt))  return HIT_MAX;
    return HIT_NONE;
}

/* The whole reason drag and resize still feel native: we do not implement
   them. We answer "what is under the cursor" with the codes Windows
   already understands, and DefWindowProc runs the move/size loop -
   including edge snapping and the double-click-to-maximise on a caption. */
static LRESULT cap_hittest(HWND hwnd, LPARAM lParam)
{
    RECT rc;
    POINT pt;
    int x, y, l, t, rr, b;

    pt.x = GET_X_LPARAM(lParam);
    pt.y = GET_Y_LPARAM(lParam);
    ScreenToClient(hwnd, &pt);
    GetClientRect(hwnd, &rc);
    x = pt.x; y = pt.y;

    l = (x < FRAME); t = (y < FRAME);
    rr = (x >= rc.right - FRAME); b = (y >= rc.bottom - FRAME);

    if (!g_maxed) {                 /* a maximised window does not resize */
        if (l && t)  return HTTOPLEFT;
        if (rr && t) return HTTOPRIGHT;
        if (l && b)  return HTBOTTOMLEFT;
        if (rr && b) return HTBOTTOMRIGHT;
        if (l)  return HTLEFT;
        if (rr) return HTRIGHT;
        if (t)  return HTTOP;
        if (b)  return HTBOTTOM;
    }
    if (cap_button_at(hwnd, pt) != HIT_NONE)
        return HTCLIENT;            /* ours; we handle the click */
    if (y < FRAME + CAP_H)
        return HTCAPTION;           /* Windows drags it for us */
    return HTCLIENT;
}

/* Square the corners off. Windows 11 rounds every top-level window,
   including a WS_POPUP one, and a rounded 1993 window looks like a
   mistake. Loaded at run time so this same binary still runs on Wine and
   on Windows 7, where the call does not exist. */
static void square_corners(HWND hwnd)
{
    HMODULE dwm = LoadLibraryA("dwmapi.dll");
    HRESULT (WINAPI *setattr)(HWND, DWORD, LPCVOID, DWORD);
    DWORD pref = 1;                 /* DWMWCP_DONOTROUND */

    if (!dwm)
        return;
    setattr = (HRESULT (WINAPI *)(HWND, DWORD, LPCVOID, DWORD))
              GetProcAddress(dwm, "DwmSetWindowAttribute");
    if (setattr)
        setattr(hwnd, 33, &pref, sizeof(pref));  /* CORNER_PREFERENCE */
    FreeLibrary(dwm);
}

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam,
                                LPARAM lParam)
{
    PAINTSTRUCT ps;
    POINT pt;

    switch (msg) {
    case WM_CREATE:
        square_corners(hwnd);
        return 0;

    /* Claim the entire window as client area, which is what removes the
       OS caption and the OS border without giving up WS_THICKFRAME - so
       resizing, snapping and minimise/maximise all still work. */
    case WM_NCCALCSIZE:
        if (wParam)
            return 0;
        break;

    case WM_NCHITTEST:
        return cap_hittest(hwnd, lParam);

    case WM_NCACTIVATE:
        /* Repaint for the active/inactive caption colour, and return TRUE
           so Windows does not try to draw a caption we do not have. */
        InvalidateRect(hwnd, NULL, FALSE);
        return TRUE;

    case WM_ACTIVATE:
        InvalidateRect(hwnd, NULL, FALSE);
        break;

    case WM_PAINT:
        BeginPaint(hwnd, &ps);
        cap_paint(hwnd, ps.hdc);
        EndPaint(hwnd, &ps);
        return 0;

    case WM_LBUTTONDOWN:
        pt.x = GET_X_LPARAM(lParam);
        pt.y = GET_Y_LPARAM(lParam);
        g_down = cap_button_at(hwnd, pt);
        if (g_down != HIT_NONE) {
            SetCapture(hwnd);
            InvalidateRect(hwnd, NULL, FALSE);
        }
        return 0;

    case WM_LBUTTONUP:
        if (g_down != HIT_NONE) {
            int was = g_down;

            pt.x = GET_X_LPARAM(lParam);
            pt.y = GET_Y_LPARAM(lParam);
            g_down = HIT_NONE;
            ReleaseCapture();
            InvalidateRect(hwnd, NULL, FALSE);
            /* Only fires if the release lands on the same button, which
               is what every real button does. */
            if (cap_button_at(hwnd, pt) == was) {
                if (was == HIT_MIN)
                    ShowWindow(hwnd, SW_MINIMIZE);
                else if (was == HIT_MAX)
                    ShowWindow(hwnd, g_maxed ? SW_RESTORE : SW_MAXIMIZE);
                else if (was == HIT_SYS)
                    /* 3.1's sysmenu box. The menu itself is not part of
                       the visual question, so the spike only proves the
                       hit lands. */
                    MessageBeep(0);
            }
        }
        return 0;

    case WM_SIZE:
        g_maxed = (wParam == SIZE_MAXIMIZED);
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;

    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASS wc;
    HWND hwnd;
    MSG msg;

    (void)prev;

    ZeroMemory(&wc, sizeof(wc));
    wc.style         = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = inst;
    wc.hCursor       = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = NULL;        /* we cover every pixel ourselves */
    wc.lpszClassName = "LLM64CapSpike";
    if (!RegisterClass(&wc))
        return 1;

    /* WS_POPUP kills the OS caption; WS_THICKFRAME keeps the sizing and
       min/max machinery, and WM_NCCALCSIZE above stops it being drawn. */
    hwnd = CreateWindow("LLM64CapSpike", "LLM64",
                        WS_POPUP | WS_THICKFRAME | WS_MINIMIZEBOX
                        | WS_MAXIMIZEBOX | WS_CLIPCHILDREN,
                        CW_USEDEFAULT, CW_USEDEFAULT, 640, 300,
                        NULL, NULL, inst, NULL);
    if (!hwnd)
        return 1;
    /* "max" on the command line starts maximised, which is the state the
       reference capture is in - the only way to compare the caption
       geometry without a sizing border in the way. */
    ShowWindow(hwnd, (cmd && strstr(cmd, "max")) ? SW_MAXIMIZE : show);
    UpdateWindow(hwnd);

    while (GetMessage(&msg, NULL, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    return (int)msg.wParam;
}
