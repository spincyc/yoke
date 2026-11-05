#pragma once
/* lwIP's BSD sockets: the host's types and constants, with every call the
 * glue makes routed to the host program's fakes (host_shell.c), which also
 * check that the network stack is up. */
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

int stub_socket(int domain, int type, int protocol);
int stub_bind(int fd, const struct sockaddr *address, socklen_t size);
int stub_listen(int fd, int backlog);
int stub_accept(int fd, struct sockaddr *address, socklen_t *size);
int stub_setsockopt(int fd, int level, int name, const void *value, socklen_t size);
int stub_fcntl(int fd, int command, ...);
ssize_t stub_recvfrom(int fd, void *buffer, size_t size, int flags, struct sockaddr *from,
                      socklen_t *from_size);
ssize_t stub_sendto(int fd, const void *buffer, size_t size, int flags,
                    const struct sockaddr *to, socklen_t to_size);
int stub_close(int fd);
#define socket stub_socket
#define bind stub_bind
#define listen stub_listen
#define accept stub_accept
#define setsockopt stub_setsockopt
#define fcntl stub_fcntl
#define recvfrom stub_recvfrom
#define sendto stub_sendto
#define close stub_close
