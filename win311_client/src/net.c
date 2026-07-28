/* LLM64 for Windows - Winsock 1.1 transport (see include/net.h) */

#include <windows.h>
#include <winsock.h>
#include <string.h>
#include <stdio.h>
#include "net.h"

#define TXQ_SIZE 4096

static HWND     g_owner   = NULL;
static SOCKET   g_sock    = INVALID_SOCKET;
static NetState g_state   = NET_IDLE;
static int      g_started = 0;

/* Outbound queue. Small on purpose: what this client sends is typed
   text and short commands, and anything that would overflow it wants
   to be a paced transfer, not a burst. */
static unsigned char g_txq[TXQ_SIZE];
static unsigned      g_txhead = 0;   /* next byte to send */
static unsigned      g_txtail = 0;   /* next free slot */

static void set_err(char *err, int errlen, const char *what, int code)
{
    if (!err || errlen <= 0)
        return;
    if (code)
        wsprintf(err, "%s (Winsock error %d)", (LPSTR)what, code);
    else
        lstrcpy(err, what);
    err[errlen - 1] = '\0';
}

int net_init(HWND owner, char *err, int errlen)
{
    WSADATA wsa;
    int rc;

    g_owner = owner;
    if (g_started)
        return 1;

    /* 1.1 is what ships with TCP/IP-32 and with Trumpet; asking for
       more would refuse to load on the machines this is aimed at. */
    rc = WSAStartup(0x0101, &wsa);
    if (rc != 0) {
        set_err(err, errlen, "No Winsock stack is loaded", rc);
        return 0;
    }
    g_started = 1;
    return 1;
}

void net_shutdown(void)
{
    net_disconnect();
    if (g_started) {
        WSACleanup();
        g_started = 0;
    }
}

NetState net_state(void)
{
    return g_state;
}

void net_disconnect(void)
{
    if (g_sock != INVALID_SOCKET) {
        WSAAsyncSelect(g_sock, g_owner, 0, 0);
        closesocket(g_sock);
        g_sock = INVALID_SOCKET;
    }
    g_txhead = g_txtail = 0;
    g_state = NET_IDLE;
}

int net_connect(const char *host, unsigned short port, char *err, int errlen)
{
    struct sockaddr_in sa;
    unsigned long addr;
    struct hostent FAR *he;
    int rc;

    net_disconnect();

    addr = inet_addr((char FAR *)host);
    if (addr == INADDR_NONE) {
        /* Blocking resolution, and the one place this program can
           stall. Acceptable because it happens once, at the user's
           request, before anything is on screen; the asynchronous
           WSAAsyncGetHostByName is the upgrade if a slow DNS ever
           makes this visible. */
        g_state = NET_RESOLVING;
        he = gethostbyname((char FAR *)host);
        if (!he) {
            set_err(err, errlen, "Cannot resolve that host name",
                    WSAGetLastError());
            g_state = NET_FAILED;
            return 0;
        }
        memcpy(&addr, he->h_addr, 4);
    }

    g_sock = socket(PF_INET, SOCK_STREAM, 0);
    if (g_sock == INVALID_SOCKET) {
        set_err(err, errlen, "Cannot create a socket", WSAGetLastError());
        g_state = NET_FAILED;
        return 0;
    }

    /* Asynchronous from before the connect: the notification for the
       connect itself arrives as FD_CONNECT. */
    if (WSAAsyncSelect(g_sock, g_owner, NET_WM_SOCKET,
                       FD_CONNECT | FD_READ | FD_WRITE | FD_CLOSE)
        == SOCKET_ERROR) {
        set_err(err, errlen, "WSAAsyncSelect failed", WSAGetLastError());
        net_disconnect();
        g_state = NET_FAILED;
        return 0;
    }

    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(port);
    sa.sin_addr.s_addr = addr;

    g_state = NET_CONNECTING;
    rc = connect(g_sock, (struct sockaddr FAR *)&sa, sizeof(sa));
    if (rc == SOCKET_ERROR) {
        rc = WSAGetLastError();
        /* Expected: the socket is non-blocking now, so the result
           arrives as FD_CONNECT. */
        if (rc != WSAEWOULDBLOCK) {
            set_err(err, errlen, "Cannot connect", rc);
            net_disconnect();
            g_state = NET_FAILED;
            return 0;
        }
    }
    return 1;
}

unsigned net_on_socket_msg(WPARAM wParam, LPARAM lParam,
                           char *err, int errlen)
{
    unsigned event = WSAGETSELECTEVENT(lParam);
    int      code  = WSAGETSELECTERROR(lParam);

    if ((SOCKET)wParam != g_sock)
        return 0;

    switch (event) {
    case FD_CONNECT:
        if (code) {
            set_err(err, errlen, "Connection refused or unreachable", code);
            net_disconnect();
            g_state = NET_FAILED;
        } else {
            g_state = NET_UP;
            net_flush();
        }
        return NET_EV_CONNECT;
    case FD_READ:
        return NET_EV_READ;
    case FD_WRITE:
        net_flush();
        return NET_EV_WRITE;
    case FD_CLOSE:
        set_err(err, errlen, "The proxy closed the connection", code);
        net_disconnect();
        return NET_EV_CLOSE;
    default:
        break;
    }
    return NET_EV_NONE;
}

int net_recv(unsigned char *buf, int max)
{
    int n;

    if (g_sock == INVALID_SOCKET)
        return -1;
    n = recv(g_sock, (char FAR *)buf, max, 0);
    if (n == SOCKET_ERROR) {
        if (WSAGetLastError() == WSAEWOULDBLOCK)
            return 0;
        return -1;
    }
    return n;
}

void net_flush(void)
{
    int n;

    if (g_sock == INVALID_SOCKET || g_state != NET_UP)
        return;
    while (g_txhead < g_txtail) {
        n = send(g_sock, (char FAR *)(g_txq + g_txhead),
                 (int)(g_txtail - g_txhead), 0);
        if (n == SOCKET_ERROR || n <= 0)
            return;             /* WSAEWOULDBLOCK: FD_WRITE will call back */
        g_txhead += (unsigned)n;
    }
    g_txhead = g_txtail = 0;
}

int net_send(const unsigned char *data, unsigned len)
{
    /* Compact first: the queue only grows while the socket is stalled,
       which on a LAN is momentary. */
    if (g_txhead > 0 && g_txhead == g_txtail)
        g_txhead = g_txtail = 0;
    if (g_txtail + len > TXQ_SIZE) {
        if (g_txhead > 0) {
            memmove(g_txq, g_txq + g_txhead, g_txtail - g_txhead);
            g_txtail -= g_txhead;
            g_txhead = 0;
        }
        if (g_txtail + len > TXQ_SIZE)
            return 0;
    }
    memcpy(g_txq + g_txtail, data, len);
    g_txtail += len;
    net_flush();
    return 1;
}
