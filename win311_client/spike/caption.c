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
#include <string.h>     /* strstr, for the command line */
#include "llmport.h"    /* the 16-bit/32-bit seam - and it brings
                           windowsx.h, GET_X_LPARAM and LLM_WORKAREA */

#define CAP_H       18      /* caption, measured */
#define BTN_W       18      /* measured: square, the caption height */
/* The sizing border, measured off File Manager on real 3.11: one black
   line, TWO rows of flat C0C0C0, one black line. Four pixels, and no
   bevel anywhere in it - the 3D look everyone remembers from 3.1 is the
   buttons, not the frame. Drawing it raised was wrong and obvious the
   moment a real window sat beside it. */
#define FRAME       4

/* How far in from each outer corner the border carries a black tick.
   Measured at 22 px on all four sides. It is not decoration: it marks
   where the corner-resize zone ends and the edge-resize zone begins, so
   the hit-test below uses the same number. */
#define CORNER      22

/* A maximised 3.1 window has NO border - it is flush to the screen, which
   is why the reference capture's sysmenu box starts at x=0. Getting this
   wrong puts a grey margin around the caption and is the first thing that
   reads as not-quite-right. */
#define FRAME_W()   (g_maxed ? 0 : FRAME)
#define RULE        1       /* the black line under the caption */
#define MENU_H      18      /* measured: same height as the caption */

/* 3.1's COLOR_MENU is WHITE. Windows 95 made it button-grey and every
   later "classic" theme kept grey, so a grey menu bar is a 1995 tell as
   loud as the caption buttons. Measured off the reference: rows y21..y38
   are pure #FFFFFF with a black rule under them. */
#define C_MENU      RGB(0xFF, 0xFF, 0xFF)
/* 3.1's COLOR_HIGHLIGHT. An open bar item and a selected popup item are
   navy with white text - NOT inverted to black, which is what this drew
   first and what reads wrong beside the real thing. */
#define C_HILITE    RGB(0x00, 0x00, 0x80)
#define C_HILITETX  RGB(0xFF, 0xFF, 0xFF)
#define C_GRAYTEXT  RGB(0x80, 0x80, 0x80)
#define MENU_PAD    8       /* per side, measured: "File" ink starts at x=9 */
/* Zero, not 3, even though the ink does start 3 rows into the bar:
   DT_TOP aligns the font's CELL to the rect, and the System font carries
   3 rows of internal leading above the ink. Setting this to the measured
   ink offset stacks the two and puts every menu title 3 rows too low. */
#define MENU_TOP    0

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

/* The bar carries the same five titles main.c uses. The POPUPS are real
   HMENUs, so TrackPopupMenu below runs them: keyboard navigation, the
   mnemonics inside a popup, highlight tracking and dismissal are all still
   the system's. We draw the bar; Windows still does the hard part. */
static const char *MENUS[] = {
    "&File", "&Link", "&Settings", "&Window", "&Help"
};
#define NMENUS  ((int)(sizeof(MENUS) / sizeof(MENUS[0])))
static HMENU g_pop[NMENUS];
static int   g_open = -1;       /* bar item with its popup showing */
/* Which bar item the mouse went down on. The popup is opened on the UP,
   not the DOWN: TrackPopupMenu with TPM_LEFTBUTTON, called while the
   button is still physically held, sees the release that follows and
   dismisses itself. Under Wine that was survivable; on real 3.11 the
   menu appears for one frame, half drawn, and vanishes. The sysmenu box
   never had the bug because caption buttons always acted on the UP. */
static int   g_bar_down = -1;

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

/* A popup entry. MF_OWNERDRAW replaces the string with a pointer, so the
   label has to live somewhere we own - hence the static tables below.
   id == 0 with no text is a separator. */
typedef struct {
    const char *text;       /* '&' marks the mnemonic */
    const char *accel;      /* right-aligned, or NULL */
    UINT        id;
} MItem;

/* 3.1's control menu, which is what the sysmenu box has always opened.
   Double-clicking the box closes the window; that is 3.1 too. */
static const MItem SYSITEMS[] = {
    { "&Restore",   NULL,     SC_RESTORE  },
    { "&Move",      NULL,     SC_MOVE     },
    { "&Size",      NULL,     SC_SIZE     },
    { "Mi&nimize",  NULL,     SC_MINIMIZE },
    { "Ma&ximize",  NULL,     SC_MAXIMIZE },
    { NULL,         NULL,     0           },
    { "&Close",     "Alt+F4", SC_CLOSE    },
};
#define NSYSITEMS ((int)(sizeof(SYSITEMS) / sizeof(SYSITEMS[0])))

static const MItem DEMOITEMS[] = {
    { "&First item",  NULL,      201 },
    { "Second &item", "Ctrl+I",  202 },
    { NULL,           NULL,      0   },
    { "A&nother",     "F5",      203 },
};
#define NDEMOITEMS ((int)(sizeof(DEMOITEMS) / sizeof(DEMOITEMS[0])))

#define POPUP_GUTTER 14     /* the checkmark column 3.1 reserves */
#define POPUP_ITEM_H 16
#define POPUP_SEP_H  6
#define POPUP_PADR   18     /* space past the accelerator */
static HMENU g_sysmenu;

/* Build a popup out of a table. MF_OWNERDRAW keeps every behaviour -
   arrow keys, mnemonics, dismissal - and hands us only the pixels. */
static HMENU popup_from(const MItem *items, int n)
{
    HMENU h = CreatePopupMenu();
    int i;

    for (i = 0; i < n; i++)
        AppendMenu(h, MF_OWNERDRAW, items[i].id, (LPCSTR)&items[i]);
    return h;
}

static void popup_measure(MEASUREITEMSTRUCT FAR *mis)
{
    const MItem FAR *it = (const MItem FAR *)mis->itemData;
    HDC hdc = GetDC(NULL);
    HFONT of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SIZE a, b;

    if (!it || !it->text) {
        mis->itemHeight = POPUP_SEP_H;
        mis->itemWidth = 0;
    } else {
        a.cx = LOWORD(GetTextExtent(hdc, it->text, lstrlen(it->text)));
        b.cx = 0;
        if (it->accel)
            b.cx = LOWORD(GetTextExtent(hdc, it->accel,
                                        lstrlen(it->accel)));
        mis->itemHeight = POPUP_ITEM_H;
        mis->itemWidth = POPUP_GUTTER + a.cx + (b.cx ? b.cx + 24 : 0)
                       + POPUP_PADR;
    }
    SelectObject(hdc, of);
    ReleaseDC(NULL, hdc);
}

static void popup_draw(DRAWITEMSTRUCT FAR *dis)
{
    const MItem FAR *it = (const MItem FAR *)dis->itemData;
    RECT r = dis->rcItem;
    int sel = (dis->itemState & ODS_SELECTED) != 0;
    int dis_ = (dis->itemState & ODS_GRAYED) != 0;
    HFONT of;

    if (!it || !it->text) {
        /* 3.1's separator is etched: a shadow line with a highlight under
           it, not a single black rule. */
        fill(dis->hDC, &r, C_MENU);
        hline(dis->hDC, r.left + 2, r.right - 3,
              (r.top + r.bottom) / 2 - 1, C_SHADOW);
        hline(dis->hDC, r.left + 2, r.right - 3,
              (r.top + r.bottom) / 2, C_HILIGHT);
        return;
    }

    fill(dis->hDC, &r, sel ? C_HILITE : C_MENU);
    SetBkMode(dis->hDC, TRANSPARENT);
    SetTextColor(dis->hDC, dis_ ? C_GRAYTEXT
                                : (sel ? C_HILITETX : C_FRAME));
    of = SelectObject(dis->hDC, GetStockObject(SYSTEM_FONT));
    r.left += POPUP_GUTTER;
    /* No DT_NOPREFIX: the '&' must become the underline. */
    DrawText(dis->hDC, it->text, -1, &r,
             DT_LEFT | DT_VCENTER | DT_SINGLELINE);
    if (it->accel) {
        RECT a = dis->rcItem;

        a.right -= POPUP_PADR;
        DrawText(dis->hDC, it->accel, -1, &a,
                 DT_RIGHT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    }
    SelectObject(dis->hDC, of);
}

/* Open the control menu under the sysmenu box, and grey what does not
   apply - Restore is dead unless maximised, Maximize is dead when it
   already is. 3.1 did exactly this. */
static void sysmenu_drop(HWND hwnd)
{
    RECT sys, mn, mx;
    POINT p;

    btn_rects(hwnd, &sys, &mn, &mx);
    EnableMenuItem(g_sysmenu, SC_RESTORE,
                   MF_BYCOMMAND | (g_maxed ? MF_ENABLED : MF_GRAYED));
    EnableMenuItem(g_sysmenu, SC_MAXIMIZE,
                   MF_BYCOMMAND | (g_maxed ? MF_GRAYED : MF_ENABLED));
    EnableMenuItem(g_sysmenu, SC_SIZE,
                   MF_BYCOMMAND | (g_maxed ? MF_GRAYED : MF_ENABLED));
    p.x = sys.left;
    p.y = sys.bottom + 1;
    ClientToScreen(hwnd, &p);
    TrackPopupMenu(g_sysmenu, TPM_LEFTALIGN | TPM_TOPALIGN | TPM_LEFTBUTTON,
                   p.x, p.y, 0, hwnd, NULL);
}

/* Item rects along the bar, laid end to end from x=1 with MENU_PAD each
   side. Windows would normally compute these; owning the bar means owning
   the arithmetic, and it is the one place a hand-drawn menu can drift out
   of step with where the popup appears. */
static int menu_layout(HWND hwnd, HDC hdc, RECT out[])
{
    RECT rc;
    HFONT of;
    int i, x, f = FRAME_W();

    GetClientRect(hwnd, &rc);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    x = f;
    for (i = 0; i < NMENUS; i++) {
        const char *lbl = MENUS[i];
        char plain[32];
        int n = 0;
        SIZE sz;

        /* Measure without the mnemonic marker - '&' is an instruction, not
           a glyph, and counting it makes every item a character too wide. */
        while (*lbl && n < (int)sizeof(plain) - 1) {
            if (*lbl != '&')
                plain[n++] = *lbl;
            lbl++;
        }
        plain[n] = '\0';
        sz.cx = LOWORD(GetTextExtent(hdc, plain, n));

        out[i].left   = x;
        out[i].right  = x + MENU_PAD * 2 + sz.cx;
        out[i].top    = f + CAP_H + RULE;
        out[i].bottom = out[i].top + MENU_H;
        x = out[i].right;
    }
    SelectObject(hdc, of);
    return NMENUS;
}

/* Just the caption strip, or just the menu bar. Repainting the whole
   window to un-press an 18x18 button is a visible flash on a real 3.11
   machine - it was invisible under Wine on a 2026 CPU. */
static void bar_rect(HWND hwnd, RECT *r, int menu)
{
    RECT rc;
    int f = FRAME_W();

    GetClientRect(hwnd, &rc);
    r->left  = f;
    r->right = rc.right - f;
    r->top   = menu ? f + CAP_H + RULE : f;
    r->bottom = menu ? r->top + MENU_H : f + CAP_H;
}

static void inval_bar(HWND hwnd, int menu)
{
    RECT r;

    bar_rect(hwnd, &r, menu);
    InvalidateRect(hwnd, &r, FALSE);
}

static void menu_paint(HWND hwnd, HDC hdc)
{
    RECT rc, bar, items[NMENUS];
    HFONT of;
    int i, f = FRAME_W();

    GetClientRect(hwnd, &rc);
    bar.left = f; bar.right = rc.right - f;
    bar.top = f + CAP_H + RULE;
    bar.bottom = bar.top + MENU_H;
    fill(hdc, &bar, C_MENU);
    hline(hdc, bar.left, bar.right - 1, bar.bottom, C_FRAME);

    menu_layout(hwnd, hdc, items);
    SetBkMode(hdc, TRANSPARENT);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    for (i = 0; i < NMENUS; i++) {
        RECT t = items[i];
        int open = (i == g_open);

        if (open) {
            /* COLOR_HIGHLIGHT, not black. */
            fill(hdc, &t, C_HILITE);
            SetTextColor(hdc, C_HILITETX);
        } else {
            SetTextColor(hdc, C_FRAME);
        }
        t.left += MENU_PAD;
        t.top  += MENU_TOP;
        /* No DT_NOPREFIX here, on purpose: the '&' has to become the
           underline, which is the only reason it is in the string. */
        DrawText(hdc, MENUS[i], -1, &t, DT_LEFT | DT_TOP | DT_SINGLELINE);
    }
    SelectObject(hdc, of);
}

/* Which bar item a client-space point is on, or -1. */
static int menu_at(HWND hwnd, POINT pt)
{
    RECT items[NMENUS];
    HDC hdc = GetDC(hwnd);
    int i, hit = -1;

    menu_layout(hwnd, hdc, items);
    ReleaseDC(hwnd, hdc);
    for (i = 0; i < NMENUS; i++)
        if (PtInRect(&items[i], pt))
            hit = i;
    return hit;
}

/* Hand the popup to Windows. Everything inside it - arrow keys, mnemonics,
   click-away, submenu timing - is the system's from here, which is the
   whole argument for skinning rather than reimplementing. */
static void menu_drop(HWND hwnd, int i)
{
    RECT items[NMENUS];
    POINT p;
    HDC hdc = GetDC(hwnd);

    menu_layout(hwnd, hdc, items);
    ReleaseDC(hwnd, hdc);
    if (i < 0 || i >= NMENUS || !g_pop[i])
        return;
    g_open = i;
    inval_bar(hwnd, 1);
    UpdateWindow(hwnd);
    p.x = items[i].left;
    p.y = items[i].bottom + 1;
    ClientToScreen(hwnd, &p);
    TrackPopupMenu(g_pop[i], TPM_LEFTALIGN | TPM_TOPALIGN | TPM_LEFTBUTTON,
                   p.x, p.y, 0, hwnd, NULL);
    g_open = -1;
    inval_bar(hwnd, 1);
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
        int a = 1, b = FRAME - 2;           /* the face rows/cols */
        int xl = rc.left + CORNER, xr = rc.right - 1 - CORNER;
        int yt = rc.top + CORNER,  yb = rc.bottom - 1 - CORNER;

        r = rc;
        fill(hdc, &r, C_FACE);
        FrameRect(hdc, &r, k);              /* outer black line */
        InflateRect(&r, -(FRAME - 1), -(FRAME - 1));
        FrameRect(hdc, &r, k);              /* inner black line */
        DeleteObject(k);

        /* Eight corner grips, two per side. */
        vline(hdc, xl, rc.top + a, rc.top + b, C_FRAME);
        vline(hdc, xr, rc.top + a, rc.top + b, C_FRAME);
        vline(hdc, xl, rc.bottom - 1 - b, rc.bottom - 1 - a, C_FRAME);
        vline(hdc, xr, rc.bottom - 1 - b, rc.bottom - 1 - a, C_FRAME);
        hline(hdc, rc.left + a, rc.left + b, yt, C_FRAME);
        hline(hdc, rc.left + a, rc.left + b, yb, C_FRAME);
        hline(hdc, rc.right - 1 - b, rc.right - 1 - a, yt, C_FRAME);
        hline(hdc, rc.right - 1 - b, rc.right - 1 - a, yb, C_FRAME);
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
    menu_paint(hwnd, hdc);

    r.left   = f;
    r.right  = rc.right - f;
    r.top    = f + CAP_H + RULE + MENU_H + 1;
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
             "The buttons work. The corners are square on Windows 11.\r\n\r\n"
             "The menu bar is ours too - white, as 3.1's is, not the grey\r\n"
             "95 changed it to. The popups are real HMENUs handed to\r\n"
             "TrackPopupMenu, so arrow keys and mnemonics still work.",
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

    if (!g_maxed && (l || t || rr || b)) {
        /* Anywhere in the border within CORNER of a corner resizes
           diagonally - which is exactly what the tick marks delimit, so
           the grip you can see and the grip you can grab agree. A 4 px
           corner (the old behaviour) is very hard to hit on purpose. */
        int nl = (x < CORNER), nr = (x >= rc.right - CORNER);
        int nt = (y < CORNER), nb = (y >= rc.bottom - CORNER);

        if (nl && nt) return HTTOPLEFT;
        if (nr && nt) return HTTOPRIGHT;
        if (nl && nb) return HTBOTTOMLEFT;
        if (nr && nb) return HTBOTTOMRIGHT;
        if (l)  return HTLEFT;
        if (rr) return HTRIGHT;
        if (t)  return HTTOP;
        return HTBOTTOM;
    }
    if (cap_button_at(hwnd, pt) != HIT_NONE)
        return HTCLIENT;            /* ours; we handle the click */
    if (y < FRAME_W() + CAP_H)
        return HTCAPTION;           /* Windows drags it for us */
    return HTCLIENT;                /* menu bar included - never HTCAPTION,
                                       or the bar would drag the window */
}

/* Square the corners off. Windows 11 rounds every top-level window,
   including a WS_POPUP one, and a rounded 1993 window looks like a
   mistake. Loaded at run time so this same binary still runs on Wine and
   on Windows 7, where the call does not exist. */
static void square_corners(HWND hwnd)
{
#ifdef __WATCOMC__
    /* Windows 3.1 has no compositor and no rounded corners to switch off. */
    (void)hwnd;
#else
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
#endif
}

long FAR PASCAL _export WndProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    PAINTSTRUCT ps;
    POINT pt;

    switch (msg) {
    case WM_CREATE: {
        int i;

        square_corners(hwnd);
        /* Real HMENUs, so the popups behave natively. In main.c these come
           straight from llm64.rc instead - the point of the spike is that
           the bar is ours and the contents are not. */
        for (i = 0; i < NMENUS; i++)
            g_pop[i] = popup_from(DEMOITEMS, NDEMOITEMS);
        g_sysmenu = popup_from(SYSITEMS, NSYSITEMS);
        return 0;
    }

    /* Claim the entire window as client area, which is what removes the
       OS caption and the OS border without giving up WS_THICKFRAME - so
       resizing, snapping and minimise/maximise all still work. */
    case WM_NCCALCSIZE:
        /* Both forms, not just wParam=TRUE. Falling through to
           DefWindowProc on the FALSE call lets it inset the rect by a
           frame we do not draw, so the two answers disagree by the frame
           width and the edge is left with a stale margin that never
           repaints. Returning 0 leaves the rect alone, which is exactly
           "the client area is the whole window". */
        return 0;

    /* A maximised WS_THICKFRAME window is sized LARGER than the work area
       on purpose - the sizing frame is meant to hang off the screen edges.
       We removed that frame in WM_NCCALCSIZE, so without this the whole
       caption slides right by the frame width and the maximise button ends
       up past the right edge of the monitor. Pinning the maximised rect to
       the work area is exact and needs no frame metrics.

       Measured as 6 px on this machine - precisely the kind of thing that
       is invisible by eye and obvious to a pixel differ. */
    case WM_GETMINMAXINFO: {
        MINMAXINFO FAR *mmi = (MINMAXINFO FAR *)lParam;
        RECT wa;

        LLM_WORKAREA(&wa);
        mmi->ptMaxPosition.x = wa.left;
        mmi->ptMaxPosition.y = wa.top;
        mmi->ptMaxSize.x = wa.right - wa.left;
        mmi->ptMaxSize.y = wa.bottom - wa.top;
        mmi->ptMaxTrackSize = mmi->ptMaxSize;
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
        /* A minimised Win16 window with no class icon is painted by its
           own WM_PAINT - so this used to draw a caption and a menu bar
           into a 32x32 icon, which is precisely what it looked like. */
        if (IsIconic(hwnd))
            break;
        BeginPaint(hwnd, &ps);
        cap_paint(hwnd, ps.hdc);
        EndPaint(hwnd, &ps);
        return 0;

    case WM_LBUTTONDOWN:
        pt.x = GET_X_LPARAM(lParam);
        pt.y = GET_Y_LPARAM(lParam);
        g_bar_down = menu_at(hwnd, pt);
        if (g_bar_down >= 0) {
            g_open = g_bar_down;        /* highlight on the press */
            inval_bar(hwnd, 1);
            return 0;
        }
        g_down = cap_button_at(hwnd, pt);
        if (g_down != HIT_NONE) {
            SetCapture(hwnd);
            inval_bar(hwnd, 0);
        }
        return 0;

    case WM_LBUTTONUP:
        if (g_bar_down >= 0) {
            int m = g_bar_down;

            g_bar_down = -1;
            pt.x = GET_X_LPARAM(lParam);
            pt.y = GET_Y_LPARAM(lParam);
            if (menu_at(hwnd, pt) == m) {
                menu_drop(hwnd, m);
            } else {
                g_open = -1;
                inval_bar(hwnd, 1);
            }
            return 0;
        }
        if (g_down != HIT_NONE) {
            int was = g_down;

            pt.x = GET_X_LPARAM(lParam);
            pt.y = GET_Y_LPARAM(lParam);
            g_down = HIT_NONE;
            ReleaseCapture();
            inval_bar(hwnd, 0);
            /* Only fires if the release lands on the same button, which
               is what every real button does. */
            if (cap_button_at(hwnd, pt) == was) {
                if (was == HIT_MIN)
                    ShowWindow(hwnd, SW_MINIMIZE);
                else if (was == HIT_MAX)
                    ShowWindow(hwnd, g_maxed ? SW_RESTORE : SW_MAXIMIZE);
                else if (was == HIT_SYS)
                    sysmenu_drop(hwnd);
            }
        }
        return 0;

    case WM_MEASUREITEM:
        popup_measure((MEASUREITEMSTRUCT *)lParam);
        return TRUE;

    case WM_DRAWITEM:
        popup_draw((DRAWITEMSTRUCT *)lParam);
        return TRUE;

    case WM_COMMAND:
        /* The control menu's items are SC_* values, so hand them straight
           back to the system and let it run the move/size/close loops. */
        if (LOWORD(wParam) >= SC_SIZE && LOWORD(wParam) <= SC_CLOSE) {
            PostMessage(hwnd, WM_SYSCOMMAND, LOWORD(wParam), 0);
            return 0;
        }
        return 0;

    case WM_LBUTTONDBLCLK:
        /* Double-clicking the sysmenu box closes the window. 3.1 again. */
        {
            POINT d;

            d.x = GET_X_LPARAM(lParam);
            d.y = GET_Y_LPARAM(lParam);
            if (cap_button_at(hwnd, d) == HIT_SYS) {
                PostMessage(hwnd, WM_CLOSE, 0, 0);
                return 0;
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

int PASCAL WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASS wc;
    HWND hwnd;
    MSG msg;

    (void)prev;

    memset(&wc, 0, sizeof(wc));
    wc.style         = CS_HREDRAW | CS_VREDRAW | CS_DBLCLKS;
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = inst;
    wc.hCursor       = LoadCursor(NULL, IDC_ARROW);
    wc.hIcon         = LoadIcon(NULL, IDI_APPLICATION);
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
