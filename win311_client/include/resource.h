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
#define IDM_THEME_PAPER 113
#define IDM_THEME_SCREEN 114
#define IDM_CLOSEPAPER  115
#define IDM_MENU        116
#define IDM_SHOWBAR     117
#define IDM_PICTURE     118
#define IDM_DEFLAYOUT   119
#define IDM_SAVEPIC     120
#define IDM_PICSET      121
#define IDM_CONVS       122

#define IDM_FIRSTCHILD  200

/* Position of the &Window popup in the menu bar:
   File, Link, Settings, Window, Help. */
#define WINDOW_MENU_POS 3

/* Server dialog */
#define IDC_HOST        1101
#define IDC_PORT        1102
#define IDC_RECONNECT   1103

/* Pictures dialog */
#define IDC_ROOMPICS    1401

/* The session shelf: the picture window's list of everything received
   this run. A control id, like ID_PANE and ID_INPUT. */
#define ID_PICLIST      1002
#define ID_INVLIST      1003

/* Menu dialog. The entry buttons are created at run time - the proxy
   decides how many there are - and numbered from IDC_MENUBASE. */
#define IDC_MENUTITLE   1200
#define IDC_MENUBASE    1210
#define MAX_MENU        16

/* The Actions window's buttons - the server-fed menu entries. (The
   name is historic: they were once a bar glued to the frame.) */
#define IDC_BARBASE     1300

/* The launcher strip across the top of the frame: one button per big
   window, click to open or close it. The named aliases exist for the
   Window menu and the Ctrl+1..6 accelerators, which toggle the same
   windows through the same WM_COMMAND ids the buttons use - one
   handler, three ways in. (These sit above IDM_FIRSTCHILD on purpose:
   the frame's handler consumes them before DefFrameProc could mistake
   them for Window-menu child picks.) */
#define IDC_LAUNCHBASE  1500
/* Slot 0 is the Menu button - the F1 dialog, not a window toggle. */
#define IDM_TOGCONV     (IDC_LAUNCHBASE + 1)
#define IDM_TOGPIC      (IDC_LAUNCHBASE + 2)
#define IDM_TOGACT      (IDC_LAUNCHBASE + 3)
#define IDM_TOGMUS      (IDC_LAUNCHBASE + 4)
#define IDM_TOGCHR      (IDC_LAUNCHBASE + 5)
#define IDM_TOGINV      (IDC_LAUNCHBASE + 6)

/* The Music window's playback controls. */
#define IDC_MUSBASE     1600

/* The Conversations browser. */
#define IDC_CONVLIST    1701
#define IDC_CONVLOAD    1702
#define IDC_CONVDEL     1703
#define IDC_CONVSTAR    1704
#define IDC_CONVMORE    1705
#define IDC_CONVNEW     1706

#endif /* RESOURCE_H */
