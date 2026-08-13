/*
 * LLM64 for Windows - the Windows 3.1 window frame, drawn by us
 *
 * The OS will not give us a 1993 window. On Windows 10 and 11 it draws an
 * Aero-descended caption with a red close button; on Windows 95 and 98 it
 * draws a 1995 one. Both are wrong, and neither is configurable. So the
 * frame is ours: WS_POPUP takes the caption away, WM_NCCALCSIZE takes the
 * border away, and everything above the client area is painted here.
 *
 * What is NOT ours, deliberately: moving, resizing, snapping,
 * minimise/maximise and the whole modal menu loop. WM_NCHITTEST answers
 * with real HT* codes and DefWindowProc runs those; TrackPopupMenu runs
 * the popups. We replaced the painting, not the window management, which
 * is why this is a few hundred lines instead of a window manager.
 *
 * Every metric in chrome.c was measured off real 3.11 captures with
 * tools/pixdiff.py, never remembered. The caption and menu bar match a
 * real 3.11 window to 50 pixels in 17,252, and all fifty of those are
 * inside letterforms - the host's System font, not our drawing.
 *
 * It compiles for both targets and renders identically on each: the
 * 16-bit NE and the 32-bit PE differ by 0 pixels out of 38,798. That is
 * what puts the 3.1 look on Windows 95 and 98, where the 32-bit build
 * cannot go at all.
 */

#ifndef CHROME_H
#define CHROME_H

#include <windows.h>

/* The style a chromed frame wants. WS_POPUP kills the OS caption;
   WS_THICKFRAME keeps the sizing and minimise/maximise machinery, and
   WM_NCCALCSIZE stops it being drawn. */
#define CHROME_STYLE (WS_POPUP | WS_THICKFRAME | WS_MINIMIZEBOX \
                      | WS_MAXIMIZEBOX | WS_CLIPCHILDREN)

/* And the class style: CS_DBLCLKS or double-clicking the sysmenu box
   never arrives, silently. */
#define CHROME_CLASS_STYLE (CS_HREDRAW | CS_VREDRAW | CS_DBLCLKS)

#define CHROME_MAX_MENUS 8

/* Take over `hwnd`. `bar` is the application's own menu resource: its
   popups and titles are read out of it, so llm64.rc stays the single
   source of truth and the command ids on the far side are unchanged.
   Pass NULL for no menu bar. Call once, from WM_CREATE. */
void chrome_init(HWND hwnd, HMENU bar);

/* How much room the chrome takes. Layout code positions its children
   inside these: chrome_top() is the frame plus caption plus rule plus
   menu bar plus rule, chrome_edge() the sizing border at the sides and
   bottom. Both change when the window is maximised, because a maximised
   3.1 window has no border at all. */
int  chrome_top(HWND hwnd);
int  chrome_edge(HWND hwnd);

/* Paint the frame, caption and menu bar. Call from WM_PAINT before
   anything else; it does not touch the client area. */
void chrome_paint(HWND hwnd, HDC hdc);

/* One call in the window proc, before the app's own switch:
 *
 *     LONG r;
 *     if (chrome_msg(hwnd, msg, wParam, lParam, &r))
 *         return r;
 *
 * Returns nonzero when the message belonged to the chrome and `result`
 * holds what the proc should return. Handles WM_NCCALCSIZE, WM_NCHITTEST,
 * WM_GETMINMAXINFO, WM_NCACTIVATE, WM_SIZE tracking, the caption buttons,
 * the menu bar, and the owner-draw messages the popups generate.
 *
 * WM_COMMAND is swallowed ONLY for ids in the system-command range
 * (0xF000 and up), which is what the control menu generates; an
 * application's own ids are far below it and always fall through.
 * WM_SYSCOMMAND is swallowed only for SC_KEYMENU, which is how Alt+Space
 * and Alt+letter arrive.
 */
int  chrome_msg(HWND hwnd, UINT msg, UINT wParam, LONG lParam, LONG *result);

/* ---- MDI child chrome -------------------------------------------- */

/* The same treatment for an MDI child, so the children inside a 1993
   frame are not wearing 2026 captions. Give the child class
   CHROME_CLASS_STYLE, then from its window proc:
 *
 *     if (chrome_child_msg(hwnd, msg, wParam, lParam, &r)) return r;
 *     ... WM_PAINT: chrome_child_paint(hwnd, hdc, is_the_active_child);
 *     ... lay the content out inside chrome_child_top/edge
 *
 * Everything else stays DefMDIChildProc's, which is what keeps the Window
 * menu, Ctrl+F4/F6, Cascade, Tile and maximise-into-frame working.
 */
/* Tell the chrome about the MDI client, so a MAXIMISED child's sysmenu
   box and restore button appear in the menu bar - which is where 3.1 puts
   them, and the one piece of MDI's behavior that cannot come free,
   because the bar is drawn rather than handed to MDI. */
void chrome_set_mdi(HWND mdiclient);

int  chrome_child_top(HWND hwnd);
int  chrome_child_edge(HWND hwnd);
void chrome_child_paint(HWND hwnd, HDC hdc, int active);
int  chrome_child_msg(HWND hwnd, UINT msg, UINT wParam, LONG lParam,
                      LONG *result);

/* ---- dialogs ------------------------------------------------------ */

/* Same idea again for a modal dialog, whose caption is otherwise the last
 * 2026-styled surface in the program. A DialogProc cannot return an
 * arbitrary value, so the result goes through DWL_MSGRESULT:
 *
 *     LONG r;
 *     if (chrome_dialog_msg(dlg, msg, wParam, lParam, &r)) {
 *         SetWindowLong(dlg, DWL_MSGRESULT, r);
 *         return TRUE;
 *     }
 *
 * The client rect is left exactly as the resource template expects, so
 * no dialog layout has to change.
 *
 * NOTE: unlike the caption and the menu bar, these metrics are inherited
 * from the frame rather than measured - there is no 3.11 capture of a
 * dialog to diff against yet.
 */
void chrome_dialog_paint(HWND dlg, HDC hdc, int active);
int  chrome_dialog_msg(HWND dlg, UINT msg, UINT wParam, LONG lParam,
                       LONG *result);

/* ---- controls ----------------------------------------------------- */

/* Skin one BUTTON: push buttons, default push buttons and checkboxes.
   The control keeps its own state, focus, mnemonic and click handling,
   and only its painting changes. Call it as often as you like - a button
   already skinned is left alone. */
void chrome_button(HWND btn);

/* Every control of a window: skins the buttons, and takes the Windows 95
   sunken well off the edits and lists so their WS_BORDER shows through
   as the flat black frame 3.1 drew.
 *
 * Dialogs get this for free - chrome_dialog_msg does it on WM_INITDIALOG
 * - so it is only worth calling by hand for controls a window creates
 * itself, or for ones a dialog builds after its WM_INITDIALOG. */
void chrome_controls(HWND parent);

/* The 3.1 button face on any DC, for code that owner-draws its own:
   black outline with the corner pixels left to the parent, a two pixel
   mitred bevel, and #C0C0C0 inside. `deflt` adds the heavy ring 3.1 puts
   round the default button. */
void chrome_button_face(HDC hdc, const RECT *bx, int pressed, int deflt);

/* And 3.1's checkbox: a flat 13x13 black box on white with a black X
   inside it. No bevel, no sunken well and no tick - the checkmark
   arrived with Windows 95. `x`,`y` is its top left corner. */
void chrome_checkbox_face(HDC hdc, int x, int y, int checked, int disabled);

/* Answer a WM_CTLCOLOR (3.1) or WM_CTLCOLOR* (Win32) on the 3.1 palette.
 * Without it a dialog is #F0F0F0 on Windows 11 inside #C0C0C0 chrome,
 * which reads as a rendering fault rather than as a style.
 *
 * This one is NOT folded into chrome_dialog_msg, and the reason is worth
 * knowing: the answer to WM_CTLCOLOR is a BRUSH, and a brush cannot come
 * back through DWL_MSGRESULT - the dialog manager does not look there
 * for these messages. It reads the dialog procedure's own return value.
 * Routed the usual way the brush arrives as TRUE, the control falls back
 * to a default, and the symptom is a label that erases white and then
 * draws its text on gray. So it goes FIRST, ahead of chrome_dialog_msg,
 * and returns directly:
 *
 *     LONG r;
 *     if (LLM_IS_CTLCOLOR(msg) && chrome_ctlcolor(msg, wParam, lParam, &r))
 *         return (BOOL)r;
 *
 * In a plain window procedure there is no such problem - return it. */
int  chrome_ctlcolor(UINT msg, UINT wParam, LONG lParam, LONG *result);

/* ---- the scrollbar ------------------------------------------------ */

/* A window's own WS_VSCROLL is drawn by the host, and the host has been
 * drawing it wrong since 1995: Windows 95 put a 50% dither in the trough
 * and reshaped the arrows, and Windows 11 draws something flat and thin
 * that shares no pixels with either. A 3.1 scrollbar is solid #C0C0C0 in
 * the trough, and its arrow is a triangle with a STEM - an arrow, not
 * the bare triangle every later Windows uses.
 *
 * So this is a control of our own. It is the only piece of chrome here
 * that has to handle behavior as well as pixels - auto-repeat on the
 * arrows and the trough, and a draggable thumb - because unlike a menu
 * or a caption button there is no system component left underneath to
 * do it.
 *
 * It talks to its parent in exactly the vocabulary a real scrollbar
 * uses: WM_VSCROLL with SB_LINEUP, SB_PAGEDOWN, SB_THUMBTRACK and the
 * rest, packed per target. A window that already handles WM_VSCROLL
 * needs no change to accept one.
 *
 * NOTE this covers OUR panes only. The scrollbars inside a LISTBOX or a
 * COMBOBOX belong to those controls and cannot be replaced without
 * owner-drawing the whole control, which is not worth it - they appear
 * one at a time and rarely beside one of these.
 */
#define CHROME_SB_W 17      /* measured: 1 black + 15 interior + 1 black */

/* Register the window class. Once, before any are created. */
int  chrome_scrollbar_init(HINSTANCE inst);

/* One vertical scrollbar, as a child of `parent`. Position it yourself:
   it does not choose where it goes. */
HWND chrome_scrollbar(HWND parent, int id);

/* What it should show. `page` is how much one screenful is, in the same
   units as pos and max, and it sets the thumb's size the way a real
   scrollbar's page does. Cheap to call on every repaint - it does
   nothing at all if none of the three changed. */
void chrome_scrollbar_set(HWND sb, long pos, long max, long page);

#endif /* CHROME_H */
