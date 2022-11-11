#ifndef _TCPCLIENT_H
#define _TCPCLIENT_H

#include "pico/stdio.h"
#include "lwip/ip_addr.h"

#define READ_BUF_LEN 1024
#define FILENAME_LENGTH 10 // filename is always in 00000.SST format,
                           // so length is always 10.
#define SERVER_PORT 557
#define SERVER_IP "40.68.254.87" // XXX read from config file
#define POLL_TIME_S 5

struct connection {
    struct tcp_pcb *pcb;
    ip_addr_t remote_addr;
    uint32_t data_len;
    uint32_t sent_len;
    bool connected;
    bool done;
    bool success;
};

bool send_file(const char *filename);

#endif /* _TCPCLIENT_H */