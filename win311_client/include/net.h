/*
 * LLM64 for Windows - transport (Winsock 1.1, asynchronous)
 *
 * Windows 3.x is cooperatively multitasked: a blocking recv() does not
 * stall this program, it stalls the entire system. So every socket
 * operation here is asynchronous and driven by window messages -
 * WSAAsyncSelect posts NET_WM_SOCKET when the socket becomes readable,
 * writable, connected or closed, and the message loop does the rest.
 *
 * Structurally this is the same program as the C64's serial layer: a
 * ring buffer filled by an interrupt and drained by the main loop. Here
 * Windows owns the loop and the "interrupt" is a posted message.
 */

#ifndef NET_H
#define NET_H

#include <windows.h>

/* Message the transport posts to the app window. wParam is the socket,
   LOWORD(lParam) the FD_* event, HIWORD(lParam) the error. */
#define NET_WM_SOCKET (WM_USER + 100)

/* Events reported by net_on_socket_msg. Deliberately not the FD_*
   values themselves: keeping winsock.h out of the UI is what lets the
   same main.c compile against a different transport later (a COM port,
   for the machine with no network card). */
#define NET_EV_NONE     0
#define NET_EV_CONNECT  1
#define NET_EV_READ     2
#define NET_EV_WRITE    3
#define NET_EV_CLOSE    4

typedef enum {
    NET_IDLE = 0,
    NET_RESOLVING,
    NET_CONNECTING,
    NET_UP,
    NET_FAILED
} NetState;

/* Winsock startup/shutdown. net_init returns 0 on failure and puts a
   readable reason in err (may be NULL). */
int  net_init(HWND owner, char *err, int errlen);
void net_shutdown(void);

int  net_connect(const char *host, unsigned short port, char *err, int errlen);
void net_disconnect(void);
NetState net_state(void);

/* Called from the window proc on NET_WM_SOCKET. Returns the FD_* event
   so the app can react (connected, closed, error). Read-readiness is
   handled by simply calling net_recv afterwards. */
unsigned net_on_socket_msg(WPARAM wParam, LPARAM lParam, char *err, int errlen);

/* Non-blocking. Returns bytes read, 0 if none available, -1 on error. */
int  net_recv(unsigned char *buf, int max);

/* Queues bytes and sends what the socket will take. Returns 0 if the
   queue is full (the caller should treat that as backpressure). */
int  net_send(const unsigned char *data, unsigned len);

/* Flush queued bytes; call on FD_WRITE. */
void net_flush(void);

#endif /* NET_H */
