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
/* Drawing primitives. Each one reproduces a structure read straight off
   the reference capture with a pixel differ, not from memory. Every
   offset below is measured. */

static void fill(HDC hdc, const RECT *r, COLORREF c)
{
    HBRUSH b = CreateSolidBrush(c);

    FillRect(hdc, r, b);
    DeleteObject(b);
}

/* Inclusive on both ends, because that is how the measured maps read. */
static void hline(HDC hdc, int x0, int x1, int y, COLORREF c)
{
    RECT r;

    r.left = x0; r.right = x1 + 1; r.top = y; r.bottom = y + 1;
    fill(hdc, &r, c);
}

static void vline(HDC hdc, int x, int y0, int y1, COLORREF c)
{
    RECT r;

    r.left = x; r.right = x + 1; r.top = y0; r.bottom = y1 + 1;
    fill(hdc, &r, c);
}

/* A 3.1 caption button: flat face, a ONE px highlight along the top and
   left, and a TWO px shadow along the right and bottom.
 *
 * The shadow is drawn after the highlight on purpose. That ordering is
 * what produces the diagonal corners the reference has - highlight wins
 * at (left, bottom-1) while shadow wins at (right, top) - and doing it
 * the other way round is a difference you can measure but never see.
 */
static void raised_box(HDC hdc, const RECT *bx, int pressed)
{
    int l = bx->left, t = bx->top, r = bx->right - 1, bo = bx->bottom - 1;
    COLORREF hi = pressed ? C_SHADOW : C_HILIGHT;
    COLORREF lo = pressed ? C_HILIGHT : C_SHADOW;

    fill(hdc, bx, C_FACE);
    hline(hdc, l, r, t, hi);
    vline(hdc, l, t, bo, hi);
    hline(hdc, l, r, bo, lo);
    vline(hdc, r, t, bo, lo);
    hline(hdc, l + 1, r, bo - 1, lo);
    vline(hdc, r - 1, t + 1, bo, lo);
}

/* The sysmenu box is NOT a button. It has no bevel at all - just the face
   colour - and only the glyph inside is drawn: a 13x3 bar with a black
   outline, a single white row inside it, and a drop shadow offset one
   pixel down and right. Every retro mock-up draws a minus sign here. */
static void sysmenu_box(HDC hdc, const RECT *bx)
{
    int l = bx->left, t = bx->top;

    fill(hdc, bx, C_FACE);
    /* shadow first, so the outline sits on top of it where they meet */
    vline(hdc, l + 15, t + 8, t + 9, C_SHADOW);
    hline(hdc, l + 3, l + 15, t + 10, C_SHADOW);
    hline(hdc, l + 2, l + 14, t + 7, C_FRAME);
    hline(hdc, l + 2, l + 14, t + 9, C_FRAME);
    vline(hdc, l + 2, t + 7, t + 9, C_FRAME);
    vline(hdc, l + 14, t + 7, t + 9, C_FRAME);
    hline(hdc, l + 3, l + 13, t + 8, C_HILIGHT);
}

/* Four rows, 7-5-3-1 px wide, solid black. Deliberately not Polygon():
   at this size the rasteriser's triangle is not the reference's shape,
   and the difference is a third of the glyph. */
static void arrow(HDC hdc, int apex_x, int top, int up)
{
    int i;

    for (i = 0; i < 4; i++) {
        int w = up ? 1 + i * 2 : 7 - i * 2;

        hline(hdc, apex_x - w / 2, apex_x + w / 2, top + i, C_FRAME);
    }
}

/* Where the three boxes sit. Measured: each arrow button has a 1 px black
   separator on its LEFT and nothing on its right, so the navy runs
   straight up to the minimise button's highlight. */
static void btn_rects(HWND hwnd, RECT *sys, RECT *mn, RECT *mx)
{
    RECT rc;
    int f = FRAME_W();

    GetClientRect(hwnd, &rc);

    sys->left  = f;                 sys->right = f + BTN_W;
    mx->right  = rc.right - f;      mx->left   = mx->right - BTN_W;
    mn->right  = mx->left - 1;      mn->left   = mn->right - BTN_W;

    sys->top = mn->top = mx->top = f;
    sys->bottom = mn->bottom = mx->bottom = f + CAP_H;
}

static void cap_paint(HWND hwnd, HDC hdc)
{
    RECT rc, r, sys, mn, mx;
    int active = (GetActiveWindow() == hwnd);
    int f = FRAME_W();
    char title[128];
    int n;
    HFONT of;

    GetClientRect(hwnd, &rc);

    /* The sizing border: a black line outside, a raised bevel inside.
       Skipped when maximised, where 3.1 has no border to grip - which is
       also why the reference cannot verify this part at all. */
    if (f) {
        HBRUSH k = CreateSolidBrush(C_FRAME);

        r = rc;
        fill(hdc, &r, C_FACE);
        FrameRect(hdc, &r, k);
        DeleteObject(k);
        r = rc;
        InflateRect(&r, -1, -1);
        hline(hdc, r.left, r.right - 1, r.top, C_HILIGHT);
        vline(hdc, r.left, r.top, r.bottom - 1, C_HILIGHT);
        hline(hdc, r.left, r.right - 1, r.bottom - 1, C_SHADOW);
        vline(hdc, r.right - 1, r.top, r.bottom - 1, C_SHADOW);
    }

    /* Caption: one flat colour, no gradient. */
    r.left = f; r.right = rc.right - f;
    r.top  = f; r.bottom = f + CAP_H;
    fill(hdc, &r, active ? C_ACTIVE : C_INACTIVE);

    hline(hdc, f, rc.right - f - 1, f + CAP_H, C_FRAME);

    btn_rects(hwnd, &sys, &mn, &mx);

    /* Title. Centred in the GAP between the two button clusters, which is
       NOT centred on the caption: the clusters are asymmetric - one box
       left, two right - so the gap's centre sits about 10 px left of the
       window's. Measured, after getting this backwards once. The
       reference's text starts at x=481 on a 1024-wide screen and only the
       gap puts it there. */
    n = GetWindowText(hwnd, title, sizeof(title) - 1);
    title[n < 0 ? 0 : n] = '\0';
    r.left   = sys.right + 1;
    r.right  = mn.left - 1;
    r.top    = f;
    r.bottom = f + CAP_H;
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, active ? C_ACTTEXT : C_INACTTEXT);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    DrawText(hdc, title, -1, &r,
             DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    SelectObject(hdc, of);

    sysmenu_box(hdc, &sys);
    if (g_down == HIT_SYS) {
        RECT p = sys;

        InflateRect(&p, -1, -1);
        FrameRect(hdc, &p, GetStockObject(GRAY_BRUSH));
    }
    raised_box(hdc, &mn, g_down == HIT_MIN);
    raised_box(hdc, &mx, g_down == HIT_MAX);

    arrow(hdc, mn.left + 8, mn.top + 7, 0);
    if (g_maxed) {
        arrow(hdc, mx.left + 8, mx.top + 4, 1);
        arrow(hdc, mx.left + 8, mx.top + 10, 0);
    } else {
        arrow(hdc, mx.left + 8, mx.top + 7, 1);
    }

    vline(hdc, sys.right, sys.top, sys.bottom - 1, C_FRAME);
    vline(hdc, mn.left - 1, mn.top, mn.bottom - 1, C_FRAME);
    vline(hdc, mx.left - 1, mx.top, mx.bottom - 1, C_FRAME);

    /* Client area, with a note so the screenshot explains itself. */
    r.left   = f;
    r.right  = rc.right - f;
    r.top    = f + CAP_H + RULE;
    r.bottom = rc.bottom - f;
    fill(hdc, &r, C_CLIENT);
    SetTextColor(hdc, RGB(0, 0, 0));
    of = SelectObject(hdc, GetStockObject(SYSTEM_FIXED_FONT));
    InflateRect(&r, -8, -8);
    DrawText(hdc,
             "Tier 2 spike.\r\n\r\n"
             "This caption is drawn by the program, not by Windows, and\r\n"
             "every metric in it was measured off a real 3.11 capture\r\n"
             "with a pixel differ rather than remembered.\r\n\r\n"
             "Drag the caption. Drag any edge or corner to resize.\r\n"
             "The buttons work. The corners are square on Windows 11.",
             -1, &r, DT_LEFT | DT_TOP | DT_NOPREFIX);
    SelectObject(hdc, of);
}

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

    /* A maximised WS_THICKFRAME window is sized LARGER than the work area
       on purpose - the sizing frame is meant to hang off the screen edges.
       We removed that frame in WM_NCCALCSIZE, so without this the whole
       caption slides right by the frame width and the maximise button ends
       up past the right edge of the monitor. Pinning the maximised rect to
       the work area is exact and needs no frame metrics.

       Measured as 6 px on this machine - precisely the kind of thing that
       is invisible by eye and obvious to a pixel differ. */
    case WM_GETMINMAXINFO: {
        MINMAXINFO *mmi = (MINMAXINFO *)lParam;
        MONITORINFO mi;

        mi.cbSize = sizeof(mi);
        if (GetMonitorInfo(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST),
                           &mi)) {
            mmi->ptMaxPosition.x = mi.rcWork.left - mi.rcMonitor.left;
            mmi->ptMaxPosition.y = mi.rcWork.top - mi.rcMonitor.top;
            mmi->ptMaxSize.x = mi.rcWork.right - mi.rcWork.left;
            mmi->ptMaxSize.y = mi.rcWork.bottom - mi.rcWork.top;
            mmi->ptMaxTrackSize = mmi->ptMaxSize;
        }
        return 0;
    }

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
