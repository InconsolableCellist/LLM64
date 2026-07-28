/*
 * LLM64 for Windows - resource identifiers
 *
 * Included by both src/main.c and src/llm64.rc, so a menu item and the
 * case that handles it cannot drift apart.
 */

#ifndef RESOURCE_H
#define RESOURCE_H

/* Commands. Everything here has to stay below IDM_FIRSTCHILD: the MDI
   client numbers the open-document entries on the Window menu from
   there, and the frame hands anything at or above it to DefFrameProc. */
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
#define IDM_SERVER      112

#define IDM_FIRSTCHILD  200

/* Position of the &Window popup in the menu bar:
   File, Link, Settings, Window, Help. */
#define WINDOW_MENU_POS 3

/* Server dialog */
#define IDC_HOST        1101
#define IDC_PORT        1102
#define IDC_RECONNECT   1103

#endif /* RESOURCE_H */
