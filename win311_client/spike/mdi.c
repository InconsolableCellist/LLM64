/*
 * Experiment: can an MDI child give up its non-client area?
 *
 * The frame's chrome is settled. The children are not: they still wear
 * whatever caption the OS draws, which is Aero on Windows 11 and 1995 on
 * Windows 95, sitting inside a 1993 frame. The two ways out are
 *
 *   (a) suppress each child's NC area the way we did the frame's, and
 *       keep MDI - so the Window menu, Ctrl+F4/F6, Cascade, Tile and
 *       maximise-into-frame all keep working for free, or
 *   (b) drop MDI and hand-roll a child manager, which is several hundred
 *       lines and has to reproduce all of the above.
 *
 * (a) is worth an hour before committing to (b). This program is that
 * hour. The captions here are deliberately rough - the question is
 * BEHAVIOURAL, and a pixel-perfect child caption proves nothing about
 * whether DefMDIChildProc will fight us.
 *
 * What it has to survive, in order of how likely it is to break:
 *
 *   1. MAXIMISE. In 3.1 a maximised child loses its caption entirely and
 *      its sysmenu box and restore arrow migrate INTO the frame's menu
 *      bar. That is MDI doing NC work on a window with no NC.
 *   2. Activation - clicking between children must repaint both captions.
 *   3. Dragging by our caption, and resizing by our edges.
 *   4. Ctrl+F4 / Ctrl+F6, the Window menu's child list, Cascade, Tile.
 *
 *   make mdispike / mdispike16
 */

#include <windows.h>
#include <string.h>
#include "llmport.h"
#include "chrome.h"

#define CHILD_CAP   18      /* the child caption, same height as the frame's */
#define CHILD_EDGE  4
#define IDM_FIRSTCHILD  100
#define IDM_TILE        50
#define IDM_CASCADE     51
#define IDM_NEWCHILD    52

static HWND  g_frame, g_mdi;
static HMENU g_windowmenu;
static int   g_nchild;
static HWND  g_last;

/* ------------------------------------------------------------------ */

static void bar(HDC hdc, const RECT *r, COLORREF c)
{
    HBRUSH b = CreateSolidBrush(c);

    FillRect(hdc, r, b);
    DeleteObject(b);
}

/* A rough 3.1 child caption. Rough on purpose - see the header. */
static void child_paint(HWND hwnd, HDC hdc)
{
    RECT rc, r;
    int active = (GetActiveWindow() == g_frame
                  && LLM_HWND(SendMessage(g_mdi, WM_MDIGETACTIVE, 0, 0L))
                     == hwnd);
    char title[64];
    HFONT of;
    int n, maxed = IsZoomed(hwnd);
    int e = maxed ? 0 : CHILD_EDGE;

    GetClientRect(hwnd, &rc);
    bar(hdc, &rc, RGB(0xC0, 0xC0, 0xC0));
    if (!maxed) {
        HBRUSH k = CreateSolidBrush(RGB(0, 0, 0));

        r = rc;
        FrameRect(hdc, &r, k);
        InflateRect(&r, -(CHILD_EDGE - 1), -(CHILD_EDGE - 1));
        FrameRect(hdc, &r, k);
        DeleteObject(k);
    }

    /* The caption. A maximised MDI child has none: MDI is supposed to
       have hidden it and moved its buttons onto the frame's menu bar. */
    if (!maxed) {
        r.left = e; r.right = rc.right - e;
        r.top = e;  r.bottom = e + CHILD_CAP;
        bar(hdc, &r, active ? RGB(0, 0, 0x80) : RGB(0xFF, 0xFF, 0xFF));
        n = GetWindowText(hwnd, title, sizeof(title) - 1);
        title[n < 0 ? 0 : n] = '\0';
        SetBkMode(hdc, TRANSPARENT);
        SetTextColor(hdc, active ? RGB(0xFF, 0xFF, 0xFF) : RGB(0, 0, 0));
        of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
        DrawText(hdc, title, -1, &r,
                 DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
        SelectObject(hdc, of);
    }

    /* Client area, with the state written into it so a screenshot says
       what happened without anyone having to remember. */
    r.left = e; r.right = rc.right - e;
    r.top = e + (maxed ? 0 : CHILD_CAP + 1);
    r.bottom = rc.bottom - e;
    bar(hdc, &r, RGB(0xFF, 0xFF, 0xFF));
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, RGB(0, 0, 0));
    of = SelectObject(hdc, GetStockObject(SYSTEM_FIXED_FONT));
    InflateRect(&r, -6, -6);
    wsprintf(title, maxed ? "MAXIMISED - no caption drawn"
                          : (active ? "active" : "inactive"));
    DrawText(hdc, title, -1, &r, DT_LEFT | DT_TOP | DT_NOPREFIX);
    SelectObject(hdc, of);
}

long FAR PASCAL _export ChildProc(HWND hwnd, UINT msg, UINT wParam,
                                         LONG lParam)
{
    PAINTSTRUCT ps;
    POINT pt;
    RECT rc;

    switch (msg) {
    /* The whole experiment in one case: claim the entire window as
       client, so there is no NC area for MDI to draw into. */
    case WM_NCCALCSIZE:
        return 0;

    case WM_NCHITTEST: {
        int e = IsZoomed(hwnd) ? 0 : CHILD_EDGE;

        pt.x = GET_X_LPARAM(lParam);
        pt.y = GET_Y_LPARAM(lParam);
        ScreenToClient(hwnd, &pt);
        GetClientRect(hwnd, &rc);
        if (e) {
            if (pt.x < e && pt.y < e) return HTTOPLEFT;
            if (pt.x >= rc.right - e && pt.y < e) return HTTOPRIGHT;
            if (pt.x < e && pt.y >= rc.bottom - e) return HTBOTTOMLEFT;
            if (pt.x >= rc.right - e && pt.y >= rc.bottom - e)
                return HTBOTTOMRIGHT;
            if (pt.x < e) return HTLEFT;
            if (pt.x >= rc.right - e) return HTRIGHT;
            if (pt.y < e) return HTTOP;
            if (pt.y >= rc.bottom - e) return HTBOTTOM;
            if (pt.y < e + CHILD_CAP) return HTCAPTION;
        }
        return HTCLIENT;
    }

    case WM_NCACTIVATE:
    case WM_MDIACTIVATE:
        InvalidateRect(hwnd, NULL, TRUE);
        break;

    case WM_PAINT:
        BeginPaint(hwnd, &ps);
        child_paint(hwnd, ps.hdc);
        EndPaint(hwnd, &ps);
        return 0;

    case WM_SIZE:
        InvalidateRect(hwnd, NULL, TRUE);
        break;
    }
    return DefMDIChildProc(hwnd, msg, wParam, lParam);
}

static HWND new_child(const char *title, int x, int y)
{
    MDICREATESTRUCT mcs;

    memset(&mcs, 0, sizeof(mcs));
    mcs.szClass = "SpikeChild";
    mcs.szTitle = title;
    mcs.hOwner  = LLM_INST(g_frame);
    mcs.x = x; mcs.y = y; mcs.cx = 260; mcs.cy = 150;
    mcs.style = 0;
    return LLM_HWND(SendMessage(g_mdi, WM_MDICREATE, 0,
                                (LONG)(LPMDICREATESTRUCT)&mcs));
}

long FAR PASCAL _export FrameProc(HWND hwnd, UINT msg, UINT wParam,
                                  LONG lParam)
{
    PAINTSTRUCT ps;
    CLIENTCREATESTRUCT ccs;
    RECT rc;
    LONG r;

    if (chrome_msg(hwnd, msg, wParam, lParam, &r))
        return r;

    switch (msg) {
    case WM_CREATE: {
        HMENU bar_ = CreateMenu();
        HMENU win = CreatePopupMenu();

        g_frame = hwnd;
        AppendMenu(win, MF_STRING, IDM_NEWCHILD, "&New Window");
        AppendMenu(win, MF_STRING, IDM_CASCADE,  "&Cascade");
        AppendMenu(win, MF_STRING, IDM_TILE,     "&Tile");
        AppendMenu(win, MF_SEPARATOR, 0, NULL);
        AppendMenu(bar_, MF_POPUP, (UINT)win, "&Window");
        g_windowmenu = win;
        chrome_init(hwnd, bar_);

        ccs.hWindowMenu  = win;
        ccs.idFirstChild = IDM_FIRSTCHILD;
        GetClientRect(hwnd, &rc);
        g_mdi = CreateWindow("MDICLIENT", NULL,
                             WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
                             chrome_edge(hwnd), chrome_top(hwnd),
                             rc.right - 2 * chrome_edge(hwnd),
                             rc.bottom - chrome_top(hwnd) - chrome_edge(hwnd),
                             hwnd, (HMENU)1, LLM_INST(hwnd),
                             (LPSTR)&ccs);
        new_child("Conversation", 10, 10);
        g_last = new_child("Picture", 120, 90);
        return 0;
    }

    case WM_PAINT:
        if (IsIconic(hwnd))
            break;
        BeginPaint(hwnd, &ps);
        chrome_paint(hwnd, ps.hdc);
        EndPaint(hwnd, &ps);
        return 0;

    case WM_SIZE:
        GetClientRect(hwnd, &rc);
        if (g_mdi)
            MoveWindow(g_mdi, chrome_edge(hwnd), chrome_top(hwnd),
                       rc.right - 2 * chrome_edge(hwnd),
                       rc.bottom - chrome_top(hwnd) - chrome_edge(hwnd),
                       TRUE);
        InvalidateRect(hwnd, NULL, TRUE);
        /* RETURN, do not break. DefFrameProc's WM_SIZE resizes the MDI
           client to fill the frame's whole client area, which undoes the
           MoveWindow above the instant we fall through to it - the client
           then covers the chrome completely and the frame looks like it
           never painted. Any MDI frame that reserves room at the top has
           to swallow WM_SIZE. */
        return 0;

    case WM_COMMAND:
        switch (LLM_CMD_ID(wParam, lParam)) {
        case IDM_NEWCHILD: {
            char t[32];

            wsprintf(t, "Window %d", ++g_nchild + 2);
            new_child(t, 30 + g_nchild * 20, 30 + g_nchild * 20);
            return 0;
        }
        case IDM_CASCADE:
            SendMessage(g_mdi, WM_MDICASCADE, 0, 0L);
            return 0;
        case IDM_TILE:
            SendMessage(g_mdi, WM_MDITILE, 0, 0L);
            return 0;
        }
        break;

    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefFrameProc(hwnd, g_mdi, msg, wParam, lParam);
}

int PASCAL WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASS wc;
    HWND hwnd;
    MSG msg;

    (void)prev;

    memset(&wc, 0, sizeof(wc));
    wc.style         = CHROME_CLASS_STYLE;
    wc.lpfnWndProc   = FrameProc;
    wc.hInstance     = inst;
    wc.hCursor       = LoadCursor(NULL, IDC_ARROW);
    wc.hIcon         = LoadIcon(NULL, IDI_APPLICATION);
    wc.hbrBackground = NULL;
    wc.lpszClassName = "LLM64MdiSpike";
    if (!RegisterClass(&wc))
        return 1;

    memset(&wc, 0, sizeof(wc));
    wc.style         = CS_HREDRAW | CS_VREDRAW | CS_DBLCLKS;
    wc.lpfnWndProc   = ChildProc;
    wc.hInstance     = inst;
    wc.hCursor       = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = NULL;
    wc.lpszClassName = "SpikeChild";
    if (!RegisterClass(&wc))
        return 1;

    hwnd = CreateWindow("LLM64MdiSpike", "LLM64", CHROME_STYLE,
                        CW_USEDEFAULT, CW_USEDEFAULT, 660, 420,
                        NULL, NULL, inst, NULL);
    if (!hwnd)
        return 1;
    /* SWP_FRAMECHANGED, or the layout never catches up with our
       WM_NCCALCSIZE. Windows computed this window's client rect once, at
       creation, before the frame knew it had no non-client area; nothing
       recomputes it on its own, so the MDI client keeps its creation
       geometry, sits at y=0 and covers the chrome completely. Forcing one
       NC recalculation costs a line and is invisible when it works. */
    {
        RECT wr;

        GetWindowRect(hwnd, &wr);
        SetWindowPos(hwnd, NULL, 0, 0, wr.right - wr.left,
                     wr.bottom - wr.top,
                     SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED);
    }
    ShowWindow(hwnd, show);
    UpdateWindow(hwnd);
    /* "max" maximises a child at startup. Ctrl+F10 is not an MDI system
       accelerator - TranslateMDISysAccel only does Ctrl+F4 and Ctrl+F6 -
       so driving this from the keyboard proved nothing. */
    if (cmd && strstr(cmd, "max") && g_last)
        SendMessage(g_mdi, WM_MDIMAXIMIZE, (UINT)g_last, 0L);

    while (GetMessage(&msg, NULL, 0, 0)) {
        if (!TranslateMDISysAccel(g_mdi, &msg)) {
            TranslateMessage(&msg);
            DispatchMessage(&msg);
        }
    }
    return (int)msg.wParam;
}
