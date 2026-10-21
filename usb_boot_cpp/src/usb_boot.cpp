// Behavioural reimplementation of the Marvell `usb_boot` host tool.
//
// The structure follows the original x86-64 binary function for function:
// same helper names, same global state, same log text and same "line numbers"
// in log output (the original prints __func__ and a source line number; those
// numbers are reproduced verbatim through LOG/ELOG).
//
//   usb_boot [Vendor ID] [Product ID] [IMAGE_DIR] [TELENT_PORT] [TELENT CLIENT CMD]

#include <errno.h>
#include <fcntl.h>
#include <libusb-1.0/libusb.h>
#include <netinet/in.h>
#include <pthread.h>
#include <semaphore.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#define LOG(line, fmt, ...) printf("%s, %d: " fmt, __func__, line, ##__VA_ARGS__)
#define ELOG(line, fmt, ...) fprintf(stderr, "%s, %d: " fmt, __func__, line, ##__VA_ARGS__)

#define IMG_BLOCK_SIZE 0x100000
#define EP_BULK_OUT 0x01
#define EP_BULK_IN 0x81
#define EP_INTR_OUT 0x02
#define EP_INTR_IN 0x82

const char* telcmds[] = {
    "EOF", "SUSP", "ABORT", "EOR", "SE",  "NOP",  "DMARK", "BRK", "IP",   "AO",   "AYT",
    "EC",  "EL",   "GA",    "SB",  "WILL", "WONT", "DO",    "DONT", "IAC", nullptr,
};

static int server_port = 8141;
static const char* attach_sem_name = "libusb-attach-semaphore";
static const char* detach_sem_name = "libusb-detach-semaphore";
static int cli_socket = -1;
static unsigned int g_vid = 0x1286;
static unsigned int g_pid = 0x8174;

static int img_path_prefix_len;
static char img_path_prefix[0x100];
static char img_name[0x140];
static unsigned char in_buf[0x400];
static unsigned char intr_in_buf[0x400];
static unsigned char sock_recv_buf[0x400];
static char* telnet_cmd_s;
static libusb_device_handle* h;
static libusb_context* ctx;
static unsigned int* imgbuf;
static struct libusb_transfer* out_transfer;
static struct libusb_transfer* in_transfer;
static struct libusb_transfer* intr_out_transfer;
static struct libusb_transfer* intr_in_transfer;
static volatile int do_exit;
static unsigned int img_size;
static int total_data_wrote;
static volatile int request_received;
static volatile int request_not_finished;
static int is_img_header_sent;
static pthread_t poll_thread;
static pthread_cond_t event_cond;
static pthread_mutex_t event_lock;
static pthread_cond_t client_cond;
static pthread_mutex_t client_lock;
static FILE* fp;
static pthread_t tcp_server;
static pthread_t server_dongle;
static sem_t* attach;
static sem_t* detach;
static volatile int g_usb_connected;
static unsigned char g_last_img_type;

static void my_usleep(useconds_t us) {
    usleep(us);
}

static libusb_device_handle* libusb_open_device_with_vid_pid_subclass(libusb_context* c,
                                                                      unsigned short vendor_id,
                                                                      unsigned short product_id,
                                                                      unsigned int* subclass) {
    struct libusb_device** devs;
    struct libusb_device* found = nullptr;
    struct libusb_device* dev;
    libusb_device_handle* handle = nullptr;
    struct libusb_device_descriptor desc;
    char serial[0x80];
    long i = 0;

    memset(&desc, 0, sizeof(desc));
    if (libusb_get_device_list(c, &devs) < 0)
        return nullptr;

    while ((dev = devs[i++]) != nullptr) {
        if (libusb_get_device_descriptor(dev, &desc) < 0)
            goto out;
        LOG(0xdc, "vid/pid: 0x%04x/0x%04x\n", desc.idVendor, desc.idProduct);
        if (desc.idVendor == vendor_id && desc.idProduct == product_id) {
            *subclass = desc.bDeviceSubClass;
            found = dev;
            LOG(0xe0, "Serial Number Str Idx: %d\n", desc.iSerialNumber);
            break;
        }
    }

    if (found) {
        if (libusb_open(found, &handle) < 0)
            handle = nullptr;
    }
    if (handle && desc.iSerialNumber &&
        libusb_get_string_descriptor_ascii(handle, desc.iSerialNumber,
                                           reinterpret_cast<unsigned char*>(serial), sizeof(serial)) >= 0) {
        LOG(0xed, "serial number: %s\n", serial);
    }
out:
    libusb_free_device_list(devs, 1);
    return handle;
}

static int find_boot_target(libusb_context* c, unsigned short vid, unsigned short pid, unsigned int* subclass) {
    char path[0x10f];
    char fname[0x120];
    uint8_t ports[8] = {0};
    FILE* f;

    h = libusb_open_device_with_vid_pid_subclass(c, vid, pid, subclass);
    if (h != nullptr && *subclass != 0xff) {
        libusb_device* dev = libusb_get_device(h);
        uint8_t bus = libusb_get_bus_number(dev);
        sprintf(path, "%d-", bus);
        uint8_t port = libusb_get_port_number(dev);
        LOG(0x103, "bus number: %d\n", bus);
        LOG(0x104, "port: %d\n", port);
        int n = libusb_get_port_numbers(dev, ports, 8);
        if (n > 0) {
            printf(" path: %d", ports[0]);
            sprintf(path + strlen(path), "%d", ports[0]);
            for (int i = 1; i < n; i++) {
                printf(".%d", ports[i]);
                sprintf(path + strlen(path), ".%d", ports[i]);
            }
        }
        path[strlen(path) + 1] = '\0';
        strncpy(fname, img_path_prefix, strlen(img_path_prefix) + 1);
        strcat(fname, "06_IMAGE");
        LOG(0x113, "store USB path to temp file %s.\n", fname);
        f = fopen(fname, "wb");
        if (f == nullptr) {
            ELOG(0x116, "failed to open file %s, err %s\n", fname, strerror(errno));
            return -5;
        }
        fwrite(path, strlen(path) + 1, 1, f);
        fclose(f);
    }
    return h == nullptr ? -5 : 0;
}

static void request_exit(int code) {
    do_exit = code;
    pthread_cond_signal(&event_cond);
    pthread_cond_signal(&client_cond);
}

static void cb_data_recv(struct libusb_transfer* transfer) {
    if (transfer->status == 0) {
        if (transfer->actual_length != 0)
            LOG(0x131, "actual data received %d\n", transfer->actual_length);
        libusb_submit_transfer(in_transfer);
    } else {
        ELOG(299, "img transfer status: %d\n", transfer->status);
        request_exit(2);
    }
}

static int submit_data_in_urb(void) {
    int r;
    libusb_fill_bulk_transfer(in_transfer, h, EP_BULK_IN, in_buf, 0x400, cb_data_recv, nullptr, 0);
    while ((r = libusb_submit_transfer(in_transfer)) == LIBUSB_ERROR_BUSY)
        my_usleep(1000);
    if (r < 0)
        ELOG(0x145, "libusb_submit_transfer err %d\n", r);
    else
        LOG(0x148, "\n");
    return r;
}

// Names are built as <IMAGE_DIR prefix><name>; "drm_erom.img" is requested as "./drm_erom.img".
static void process_img_request(unsigned char type) {
    char hex[8];

    if (type == 2) {
        strncpy(img_name, img_path_prefix, strlen(img_path_prefix) + 1);
        strcat(img_name, "sysinit.img");
        request_received = 1;
        is_img_header_sent = 0;
        LOG(0x157, "received request from dongle to send image %s.\n", img_name);
        pthread_cond_signal(&event_cond);
        return;
    }
    if (type == 1)
        return;
    if (type == 3) {
        strncpy(img_name, img_path_prefix, strlen(img_path_prefix) + 1);
        strcat(img_name, "bootloader.img");
        request_received = 1;
        is_img_header_sent = 0;
        LOG(0x15f, "received request from dongle to send image %s.\n", img_name);
        pthread_cond_signal(&event_cond);
        return;
    }
    if (type == 5) {
        strncpy(img_name, img_path_prefix, strlen(img_path_prefix) + 1);
        strcat(img_name, "./drm_erom.img");
        request_received = 1;
        is_img_header_sent = 0;
        LOG(0x167, "received request from dongle to send image %s.\n", img_name);
        pthread_cond_signal(&event_cond);
        return;
    }
    strncpy(img_name, img_path_prefix, strlen(img_path_prefix) + 1);
    snprintf(hex, 3, "%02x", type);
    strcat(img_name, hex);
    strcat(img_name, "_IMAGE");
    request_received = 1;
    is_img_header_sent = 0;
    LOG(0x172, "received request from dongle to send image %s.\n", img_name);
    pthread_cond_signal(&event_cond);
}

static void process_rcvd_buf(struct libusb_transfer* transfer) {
    char* p = reinterpret_cast<char*>(transfer->buffer);
    int len;
    size_t n = 0;

    for (len = transfer->actual_length; 0 < len; len = (len - (int)n) - 1) {
        char* m = strstr(p, "i*m*g*r*q*");
        if (m != nullptr) {
            g_last_img_type = m[10];
            process_img_request(g_last_img_type);
            memmove(m, m + 11, (long)len - 11);
            transfer->actual_length -= 11;
        }
        n = strlen(p);
        p += n + 1;
        n = strlen(p);
    }
    send(cli_socket, transfer->buffer, transfer->actual_length, MSG_NOSIGNAL);
    memset(transfer->buffer, 0, transfer->actual_length);
}

static void cb_intr_in(struct libusb_transfer* transfer) {
    if (transfer->status == 0) {
        if (0 < transfer->actual_length) {
            LOG(0x19d, "received len %d\n", transfer->actual_length);
            process_rcvd_buf(transfer);
        }
        if (do_exit == 0) {
            int r = libusb_submit_transfer(intr_in_transfer);
            if (r < 0)
                ELOG(0x1a6, "libusb_submit_transfer error %d\n", r);
        }
    } else {
        ELOG(0x196, "img transfer status %d?\n", transfer->status);
        intr_in_transfer = nullptr;
        request_exit(2);
    }
}

static int submit_intr_in_urb(void) {
    int r;
    libusb_fill_interrupt_transfer(intr_in_transfer, h, EP_INTR_IN, intr_in_buf, 0x400, cb_intr_in, nullptr, 0);
    while ((r = libusb_submit_transfer(intr_in_transfer)) == LIBUSB_ERROR_BUSY)
        my_usleep(1000);
    if (r < 0)
        ELOG(0x1b8, "libusb_submit_transfer err %d\n", r);
    else
        LOG(0x1bb, "\n");
    return r;
}

static void cb_intr_out(struct libusb_transfer* transfer) {
    if (transfer->status == 0) {
        LOG(0x1c8, "actual data wrote %d\n", transfer->actual_length);
    } else {
        ELOG(0x1c2, "img transfer status %d?\n", transfer->status);
        intr_out_transfer = nullptr;
        request_exit(2);
    }
}

static int submit_intr_out_urb(unsigned char* buf, int len) {
    int r;
    libusb_fill_interrupt_transfer(intr_out_transfer, h, EP_INTR_OUT, buf, len, cb_intr_out, nullptr, 0);
    while ((r = libusb_submit_transfer(intr_out_transfer)) == LIBUSB_ERROR_BUSY)
        my_usleep(1000);
    if (r < 0)
        ELOG(0x1d9, "libusb_submit_transfer err %d\n", r);
    return r;
}

static unsigned int get_img_size(void) {
    unsigned int size = 0;
    char fname[0x127];
    FILE* f;

    fseek(fp, 0, SEEK_END);
    size = (unsigned int)ftell(fp);
    fseek(fp, 0, SEEK_SET);
    LOG(0x1e5, "size %d\n", size);
    if (g_last_img_type > 0x79) {
        strncpy(fname, img_path_prefix, strlen(img_path_prefix) + 1);
        strcat(fname, "07_IMAGE");
        LOG(0x1eb, "store requested image size to temp file %s.\n", fname);
        f = fopen(fname, "wb");
        if (f == nullptr) {
            ELOG(0x1ef, "failed to open file %s, err %s\n", fname, strerror(errno));
            return (unsigned int)-5;
        }
        fwrite(&size, 4, 1, f);
        fclose(f);
    }
    return size;
}

static unsigned long feed_buf(void) {
    if (is_img_header_sent == 0) {
        *imgbuf = img_size;
        LOG(0x1fd, "sending image header package, size %d.\n", img_size);
        return 8;
    }
    size_t n = fread(imgbuf, 1, IMG_BLOCK_SIZE, fp);
    LOG(0x202, "read %d bytes from image file.\n", (unsigned int)n);
    return (unsigned int)n;
}

static void cb_img_write(struct libusb_transfer* transfer);

static int write_image(int len) {
    int r;
    out_transfer->length = len;
    libusb_fill_bulk_transfer(out_transfer, h, EP_BULK_OUT, reinterpret_cast<unsigned char*>(imgbuf),
                              out_transfer->length, cb_img_write, nullptr, 0);
    while ((r = libusb_submit_transfer(out_transfer)) == LIBUSB_ERROR_BUSY)
        my_usleep(1000);
    if (r < 0)
        ELOG(0x217, "libusb_submit_transfer err %d\n", r);
    return r;
}

static void send_next_image_block(void) {
    int n = (int)feed_buf();
    if (n != 0) {
        if (write_image(n) < 0)
            ELOG(0x224, "Error, write iamge failed\n");
    }
}

static void cb_img_write(struct libusb_transfer* transfer) {
    if (transfer->status == 0) {
        if (is_img_header_sent == 0)
            is_img_header_sent = 1;
        else
            total_data_wrote += transfer->actual_length;
        LOG(0x23a, "actual data wrote %d, total_data_wrote %d\n", transfer->actual_length, total_data_wrote);
        if ((unsigned int)total_data_wrote < img_size) {
            if (do_exit == 0) {
                request_not_finished = 1;
                send_next_image_block();
            }
        } else {
            total_data_wrote = 0;
            request_not_finished = 0;
            fclose(fp);
        }
    } else {
        ELOG(0x22e, "img transfer status %d?\n", transfer->status);
        request_exit(2);
    }
}

static int send_first_image_block(char* path) {
    int r = 0;
    fp = fopen(path, "rb");
    if (fp == nullptr) {
        ELOG(0x250, "failed to open image file %s, err %s\n", path, strerror(errno));
        return -1;
    }
    img_size = get_img_size();
    int n = (int)feed_buf();
    if (n != 0) {
        r = write_image(n);
        if (r < 0)
            ELOG(0x25a, "Error, write iamge failed\n");
    }
    return r;
}

static void sighandler(int) {
    request_exit(1);
    sem_post(attach);
    sem_post(detach);
    pthread_kill(tcp_server, SIGALRM);
}

static void* poll_thread_main(void*) {
    LOG(0x272, "poll thread running\n");
    while (do_exit == 0) {
        struct timeval tv = {1, 0};
        if (libusb_handle_events_timeout(ctx, &tv) < 0) {
            request_exit(2);
            break;
        }
    }
    LOG(0x27d, "poll thread shutting down\n");
    return nullptr;
}

static int create_listen_socket(unsigned int port) {
    int on;
    struct sockaddr_in addr;
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        ELOG(0x2b4, "create socket err %s\n", strerror(errno));
        return -1;
    }
    on = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    addr.sin_addr.s_addr = 0;
    if (bind(fd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) != 0) {
        ELOG(0x2bf, "binding tcp socket %d, err %s\n", port, strerror(errno));
        return -1;
    }
    if (listen(fd, 1) == -1) {
        ELOG(0x2c3, "listen tcp socket %d, err %s\n", port, strerror(errno));
        return -1;
    }
    return fd;
}

static int send_telnet_cmd(int fd, unsigned int cmd, unsigned int opt) {
    unsigned char buf[8];
    unsigned char* p = buf;

    LOG(0x2d1, "sending %d, opt %d\n", cmd, opt);
    *p++ = 0xff;
    if (cmd < 0x100 && cmd > 0xeb) {
        *p++ = (unsigned char)cmd;
        if (cmd == 0xfe || cmd == 0xfd || cmd == 0xfc || cmd == 0xfb)
            *p++ = (unsigned char)opt;
        return send(fd, buf, p - buf, 0) < 1 ? -1 : 0;
    }
    return -1;
}

static void init_telnet_conn(int fd) {
    send_telnet_cmd(fd, 0xfd, 0);
    send_telnet_cmd(fd, 0xfc, 1);
    send_telnet_cmd(fd, 0xfd, 3);
    send_telnet_cmd(fd, 0xfb, 0);
    send_telnet_cmd(fd, 0xfb, 3);
}

static int process_recved_telnet_cmd(int fd, unsigned char cmd) {
    // The original receives into c[0] but tests c[1] (never written) while scanning
    // a subnegotiation; c[1] is zero-initialised here instead of stack garbage.
    char c[2] = {0, 0};
    int n;

    switch (cmd) {
    case 0xf6:
        send_telnet_cmd(fd, 0xf1, 0);
        break;
    case 0xfa:
        for (int i = 0; i < 0x80; i++) {
            n = (int)recv(fd, &c[0], 1, 0);
            if (n < 1) {
                ELOG(0x30e, "recv failed, err %s\n", strerror(errno));
                return -1;
            }
            LOG(0x312, "recved %d, char %c\n", (int)c[0], (int)c[0]);
            if (c[1] == -1) {
                n = (int)recv(fd, &c[0], 1, 0);
                if (n > 0) {
                    LOG(0x31a, "recved %d, char %c\n", (int)c[0], (int)c[0]);
                    return 0;
                }
                ELOG(0x316, "recv failed, err %s\n", strerror(errno));
                return -1;
            }
        }
        break;
    case 0xfb:
    case 0xfd:
        n = (int)recv(fd, &c[0], 1, 0);
        if (n < 1) {
            ELOG(0x2f8, "recv failed, err %s\n", strerror(errno));
            return -1;
        }
        LOG(0x2fc, "recved cmd %d, opt %d\n", cmd, (int)c[0]);
        send_telnet_cmd(fd, cmd + 1, 1);
        break;
    case 0xfc:
    case 0xfe:
        n = (int)recv(fd, &c[0], 1, 0);
        if (n < 1) {
            ELOG(0x303, "recv failed, err %s\n", strerror(errno));
            return -1;
        }
        LOG(0x307, "recved cmd %d, opt %d\n", cmd, (int)c[0]);
        send_telnet_cmd(fd, cmd, (int)c[0]);
        break;
    }
    return 0;
}

static int telnet_getchar(int fd, unsigned char* out) {
    unsigned char c;
    int n;

    for (;;) {
        n = (int)recv(fd, &c, 1, 0);
        if (n < 1) {
            ELOG(0x32a, "recv failed, err %s\n", strerror(errno));
            return -1;
        }
        LOG(0x32e, "recved %d, char %c\n", c, c);
        if (c == 0xd || c == 0 || c == 10) {
            *out = 10;
            return 0;
        }
        if (c != 0xff) {
            *out = c;
            return 0;
        }
        n = (int)recv(fd, &c, 1, 0);
        if (n < 1)
            break;
        LOG(0x33c, "recved %d, char %c\n", c, c);
        if (c != 0xff && (n = process_recved_telnet_cmd(fd, c)) < 0)
            return -1;
    }
    ELOG(0x338, "recv failed, err %s\n", strerror(errno));
    return -1;
}

static void filter_out_substring(char* s, int* len, const char* sub) {
    char* p = s;
    while ((p = strstr(p, sub)) != nullptr) {
        size_t n = strlen(sub);
        size_t rest = strlen(p + n);
        memmove(p, p + n, rest + 1);
        *len -= (int)strlen(sub);
    }
}

static void* tcp_server_func(void* arg) {
    unsigned int port = *static_cast<unsigned int*>(arg);
    int listen_fd = create_listen_socket(port);
    unsigned char ch = 0;
    int len;
    int r;

    if (listen_fd < 0) {
        fprintf(stderr, "%s, %d: create socket", __func__, 0x35e);
        return nullptr;
    }
    while (do_exit == 0) {
    again:
        LOG(0x365, "server listening on port %d\n", port);
        cli_socket = accept(listen_fd, nullptr, nullptr);
        if (cli_socket < 0) {
            ELOG(0x368, "accept tcp socket %d, err %s\n", port, strerror(errno));
            return nullptr;
        }
        LOG(0x36b, "client connected.\n");
        pthread_cond_signal(&client_cond);
        init_telnet_conn(cli_socket);
        while (do_exit != 1) {
            len = 0;
            do {
                r = telnet_getchar(cli_socket, &ch);
                if (r < 0) {
                    ELOG(0x374, "client disconnected.\n");
                    goto again;
                }
                sock_recv_buf[len++] = ch;
            } while (ch != '\n' && len < 0x400);
            if (g_usb_connected != 0 && 0 < len) {
                LOG(0x37b, "sending %s, len %d to dongle.\n", sock_recv_buf, len);
                filter_out_substring(reinterpret_cast<char*>(sock_recv_buf), &len, "PuTTY");
                r = submit_intr_out_urb(sock_recv_buf, len);
                if (r != 0) {
                    ELOG(899, "send to dongle device failed.\n");
                    request_exit(2);
                }
            }
        }
        close(cli_socket);
        cli_socket = -1;
    }
    close(listen_fd);
    return nullptr;
}

static int allocate_transfer(void) {
    out_transfer = libusb_alloc_transfer(0);
    if (out_transfer == nullptr) {
        ELOG(0x39c, "libusb_alloc_transfer error\n");
        return -1;
    }
    in_transfer = libusb_alloc_transfer(0);
    if (in_transfer == nullptr) {
        ELOG(0x3a1, "libusb_alloc_transfer error\n");
        goto free_out;
    }
    intr_in_transfer = libusb_alloc_transfer(0);
    if (intr_in_transfer == nullptr) {
        ELOG(0x3a7, "libusb_alloc_transfer error\n");
        goto free_in;
    }
    intr_out_transfer = libusb_alloc_transfer(0);
    if (intr_out_transfer == nullptr) {
        ELOG(0x3ad, "libusb_alloc_transfer error\n");
        libusb_free_transfer(intr_in_transfer);
        intr_in_transfer = nullptr;
        goto free_in;
    }
    libusb_fill_bulk_transfer(out_transfer, h, EP_BULK_OUT, reinterpret_cast<unsigned char*>(imgbuf), 8,
                              cb_img_write, nullptr, 0);
    libusb_fill_bulk_transfer(in_transfer, h, EP_BULK_IN, in_buf, 0x400, cb_data_recv, nullptr, 0);
    libusb_fill_interrupt_transfer(intr_in_transfer, h, EP_INTR_IN, intr_in_buf, 0x400, cb_intr_in, nullptr, 0);
    libusb_fill_interrupt_transfer(intr_out_transfer, h, EP_INTR_OUT, sock_recv_buf, 0x400, cb_intr_out, nullptr,
                                   0);
    return 0;
free_in:
    libusb_free_transfer(in_transfer);
    in_transfer = nullptr;
free_out:
    libusb_free_transfer(out_transfer);
    out_transfer = nullptr;
    return -1;
}

static void release_transfer(void) {
    if (in_transfer != nullptr) {
        libusb_free_transfer(in_transfer);
        in_transfer = nullptr;
    }
    if (out_transfer != nullptr) {
        libusb_free_transfer(out_transfer);
        out_transfer = nullptr;
    }
    if (intr_in_transfer != nullptr) {
        libusb_free_transfer(intr_in_transfer);
        intr_in_transfer = nullptr;
    }
    if (intr_out_transfer != nullptr) {
        libusb_free_transfer(intr_out_transfer);
        intr_out_transfer = nullptr;
    }
}

static int submit_first_urbs(void) {
    int r = submit_data_in_urb();
    if (r < 0) {
        ELOG(0x3d6, "libusb_submit_transfer error %d\n", r);
    } else {
        r = submit_intr_in_urb();
        if (r < 0)
            ELOG(0x3db, "libusb_submit_transfer error %d\n", r);
    }
    return r;
}

static int serve_irom_stage_dongle(void) {
    int r = libusb_detach_kernel_driver(h, 0);
    if (r < 0)
        ELOG(0x3e6, "libusb_detach_kernel_driver error %d, ignore it\n", r);
    r = libusb_claim_interface(h, 0);
    if (r < 0) {
        ELOG(0x3eb, "usb_claim_interface error %d\n", r);
    } else {
        LOG(0x3ee, "claimed interface\n");
        out_transfer = libusb_alloc_transfer(0);
        if (out_transfer == nullptr) {
            ELOG(0x3f2, "libusb_alloc_transfer error\n");
        } else {
            is_img_header_sent = 1;
            request_not_finished = 1;
            strncpy(img_name, img_path_prefix, strlen(img_path_prefix) + 1);
            strcat(img_name, "bcm_erom.bin.usb");
            if (send_first_image_block(img_name) < 0) {
                ELOG(0x3fc, "Error, write image failed\n");
            } else {
                for (;;) {
                    if (do_exit == 0 && request_not_finished != 0) {
                        struct timeval tv = {1, 0};
                        if (libusb_handle_events_timeout(ctx, &tv) < 0) {
                            request_exit(2);
                            break;
                        }
                        LOG(0x407, "request_not_finished %d\n", request_not_finished);
                        continue;
                    }
                    break;
                }
                is_img_header_sent = 0;
                if (out_transfer != nullptr && libusb_cancel_transfer(out_transfer) < 0)
                    request_exit(2);
            }
            libusb_free_transfer(out_transfer);
            out_transfer = nullptr;
        }
        LOG(0x416, "libusb_release_interface\n");
        libusb_release_interface(h, 0);
    }
    LOG(0x419, "libusb_close -- %p\n", (void*)h);
    libusb_close(h);
    h = nullptr;
    return 0;
}

static int serve_one_target(void) {
    int r = 0;
    unsigned int subclass = 0;
    const char* msg;

    do_exit = 0;
    LOG(0x429, "libusb_init\n");
    r = libusb_init(&ctx);
    if (r < 0) {
        ELOG(0x42d, "failed to initialise libusb\n");
        exit(1);
    }
    LOG(0x431, "libusb_open_device_with_vid_pid_subclass\n");
    r = find_boot_target(ctx, g_vid, g_pid, &subclass);
    if (r < 0) {
        ELOG(0x434, "Could not find/open device %x %x\n", g_vid, g_pid);
    } else {
        LOG(0x438, "sub_class: %d\n", subclass);
        if (subclass == 0xff) {
            LOG(0x43a, "iROM stage dongle connected.\n");
            r = serve_irom_stage_dongle();
        } else {
            r = libusb_detach_kernel_driver(h, 0);
            if (r < 0)
                ELOG(0x441, "libusb_detach_kernel_driver error %d, ignore it\n", r);
            LOG(0x444, "claimed interface\n");
            r = libusb_claim_interface(h, 0);
            if (r < 0) {
                ELOG(0x447, "usb_claim_interface error %d\n", r);
            } else {
                LOG(1099, "pthread_create poll_thread_main\n");
                r = pthread_create(&poll_thread, nullptr, poll_thread_main, nullptr);
                if (r == 0) {
                    LOG(0x451, "allocate_transfer\n");
                    r = allocate_transfer();
                    if (r < 0) {
                        request_exit(2);
                        pthread_join(poll_thread, nullptr);
                    } else {
                        LOG(0x459, "submit_first_urbs\n");
                        r = submit_first_urbs();
                        if (r < 0) {
                            request_exit(2);
                        } else {
                            g_usb_connected = 1;
                            if (0 < cli_socket) {
                                msg = "one target device connected.\n\r";
                                send(cli_socket, msg, strlen(msg) + 1, MSG_NOSIGNAL);
                            }
                            while (do_exit == 0) {
                                pthread_mutex_lock(&event_lock);
                                pthread_cond_wait(&event_cond, &event_lock);
                                pthread_mutex_unlock(&event_lock);
                                if (request_received != 0 && request_not_finished == 0) {
                                    LOG(0x46c, "start processing image %s request.\n", img_name);
                                    r = send_first_image_block(img_name);
                                    if (r < 0) {
                                        ELOG(0x46f, "Error, write image failed\n");
                                        break;
                                    }
                                    request_received = 0;
                                }
                            }
                            g_usb_connected = 0;
                            if (0 < cli_socket) {
                                msg = "target device disconnected.\n\r";
                                send(cli_socket, msg, strlen(msg) + 1, MSG_NOSIGNAL);
                            }
                        }
                        pthread_join(poll_thread, nullptr);
                    }
                }
                libusb_release_interface(h, 0);
            }
            libusb_close(h);
            h = nullptr;
            release_transfer();
        }
    }
    libusb_exit(ctx);
    ctx = nullptr;
    return r;
}

static void* service_thread_func(void*) {
    while (do_exit != 1) {
        sem_wait(attach);
        if (do_exit == 1)
            break;
        serve_one_target();
        sem_wait(detach);
    }
    return nullptr;
}

static int attach_cb(libusb_context*, libusb_device* dev, libusb_hotplug_event, void*) {
    struct libusb_device_descriptor desc;

    LOG(0x4b8, "attach\n");
    if (libusb_get_device_descriptor(dev, &desc) < 0) {
        ELOG(0x4bb, "device not found in libsub.\n");
        return 1;
    }
    LOG(0x4be, "vid/pid: 0x%04x/0x%04x\n", desc.idVendor, desc.idProduct);
    my_usleep(1000000);
    if (desc.idVendor == g_vid && desc.idProduct == g_pid)
        sem_post(attach);
    return 0;
}

static int detach_cb(libusb_context*, libusb_device* dev, libusb_hotplug_event, void*) {
    struct libusb_device_descriptor desc;

    LOG(0x4cb, "detach\n");
    if (libusb_get_device_descriptor(dev, &desc) < 0) {
        ELOG(0x4ce, "device not found in libsub.\n");
        return 1;
    }
    LOG(0x4d1, "vid/pid: 0x%04x/0x%04x\n", desc.idVendor, desc.idProduct);
    if (desc.idVendor == g_vid && desc.idProduct == g_pid)
        sem_post(detach);
    return 0;
}

static void cmd_background(char* cmd) {
    if (cmd != nullptr) {
        int len = (int)strlen(cmd);
        char* full = static_cast<char*>(malloc(len + 0x10));
        if (full != nullptr) {
            memcpy(full, cmd, len);
            memcpy(full + len, "&", 2);
            system(full);
            free(full);
        }
    }
}

static int create_sem(void) {
    if (sem_unlink(attach_sem_name) != 0)
        perror("sem_unlink");
    attach = sem_open(attach_sem_name, O_CREAT, 2, 0);
    if (attach == SEM_FAILED) {
        perror("sem_open");
        return -1;
    }
    if (sem_unlink(detach_sem_name) != 0)
        perror("sem_unlink");
    detach = sem_open(detach_sem_name, O_CREAT, 2, 0);
    if (detach == SEM_FAILED) {
        perror("sem_open");
        sem_unlink(attach_sem_name);
        return -1;
    }
    return 0;
}

static void destroy_sem(void) {
    sem_unlink(detach_sem_name);
    sem_unlink(attach_sem_name);
}

static void polling_for_hotplug_event(void) {
    libusb_context* hctx = nullptr;
    libusb_hotplug_callback_handle cb_attach;
    libusb_hotplug_callback_handle cb_detach;

    LOG(0x538, "libusb_init\n");
    if (libusb_init(&hctx) < 0) {
        ELOG(0x53b, "failed to initialise libusb\n");
        return;
    }
    if (!libusb_has_capability(LIBUSB_CAP_HAS_HOTPLUG)) {
        ELOG(0x540, "Hotplug capabilites are not supported on this platform\n");
        return;
    }
    if (libusb_hotplug_register_callback(hctx, LIBUSB_HOTPLUG_EVENT_DEVICE_ARRIVED, (libusb_hotplug_flag)0,
                                         LIBUSB_HOTPLUG_MATCH_ANY, LIBUSB_HOTPLUG_MATCH_ANY,
                                         LIBUSB_HOTPLUG_MATCH_ANY, attach_cb, nullptr, &cb_attach) != 0) {
        ELOG(0x547, "Error registering callback 0\n");
        return;
    }
    if (libusb_hotplug_register_callback(hctx, LIBUSB_HOTPLUG_EVENT_DEVICE_LEFT, (libusb_hotplug_flag)0,
                                         LIBUSB_HOTPLUG_MATCH_ANY, LIBUSB_HOTPLUG_MATCH_ANY,
                                         LIBUSB_HOTPLUG_MATCH_ANY, detach_cb, nullptr, &cb_detach) != 0) {
        ELOG(0x54d, "Error registering callback 1\n");
        return;
    }
    while (do_exit != 1) {
        struct timeval tv = {1, 0};
        if (libusb_handle_events_timeout(hctx, &tv) < 0)
            break;
    }
    LOG(0x559, "event handling finished do_exit = %d\n", do_exit);
    libusb_exit(hctx);
}

int main(int argc, char** argv) {
    struct sigaction sa;
    int r;

    if (argc < 5) {
        fprintf(stderr, "%s, %d: Usage:%s [Vendor ID] [Product ID] [IMAGE_DIR] [TELENT_PORT] [TELENT CLIENT CMD]\n",
                __func__, 0x566, argv[0]);
        g_vid = 0x1286;
        g_pid = 0x8174;
        img_path_prefix_len = 2;
        memcpy(img_path_prefix, "./", 3);
        server_port = 8141;
    } else {
        g_vid = (unsigned int)strtol(argv[1], nullptr, 16);
        g_pid = (unsigned int)strtol(argv[2], nullptr, 16);
        printf("%s, %d: verder_id/product_id: 0x%04x/%04x\n", __func__, 0x572, g_vid, g_pid);
        img_path_prefix_len = (int)strlen(argv[3]);
        if ((unsigned int)(img_path_prefix_len + 1) > 0x100) {
            fprintf(stderr, "%s, %d: image path name too long\n", __func__, 0x576);
            exit(0);
        }
        memcpy(img_path_prefix, argv[3], img_path_prefix_len + 1);
        server_port = atoi(argv[4]);
    }
    if (argc == 6)
        telnet_cmd_s = argv[5];

    sa.sa_handler = sighandler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);
    sigaction(SIGQUIT, &sa, nullptr);

    pthread_cond_init(&event_cond, nullptr);
    pthread_mutex_init(&event_lock, nullptr);
    pthread_cond_init(&client_cond, nullptr);
    pthread_mutex_init(&client_lock, nullptr);

    imgbuf = static_cast<unsigned int*>(malloc(0x100004));
    if (imgbuf == nullptr) {
        fprintf(stderr, "%s, %d: failed to malloc, err %s\n", __func__, 0x596, strerror(errno));
        exit(1);
    }
    if (create_sem() != 0)
        exit(1);

    r = pthread_create(&tcp_server, nullptr, tcp_server_func, &server_port);
    if (r < 0) {
        fprintf(stderr, "%s, %d: pthread_create err, %s\n", __func__, 0x5a2, strerror(errno));
    } else {
        printf("%s, %d: wait for connection on port: %d\n", __func__, 0x5a6, server_port);
        cmd_background(telnet_cmd_s);
        printf("%s, %d: telnet client started\n", __func__, 0x5a9);
        pthread_mutex_lock(&client_lock);
        pthread_cond_wait(&client_cond, &client_lock);
        pthread_mutex_unlock(&client_lock);
        r = pthread_create(&server_dongle, nullptr, service_thread_func, nullptr);
        if (r < 0) {
            fprintf(stderr, "%s, %d: pthread_create err, %s\n", __func__, 0x5b2, strerror(errno));
        } else {
            polling_for_hotplug_event();
            pthread_join(server_dongle, nullptr);
        }
        pthread_join(tcp_server, nullptr);
    }
    destroy_sem();
    free(imgbuf);
    return 0;
}
