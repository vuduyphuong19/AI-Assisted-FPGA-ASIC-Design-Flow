/*
 * uart_apb_test.c - bare-metal board test for uart_apb on ZCU104 (Cortex-A53, Vitis standalone).
 *
 * Prints a banner on the uart_apb line (txd), then echoes every byte received on rxd, with progress
 * on the PS console (xil_printf, the board's usual PS UART).
 *
 * IMPORTANT: the AXI-APB bridge turns PSLVERR into an AXI SLVERR. On the A53 that is a data abort,
 * so this driver never triggers a spec'd error response. It checks STATUS before every TXDATA
 * write (REQ-6) and RXDATA read (REQ-16), never writes BAUD < 4 (REQ-22), and uses only offsets
 * 0x00..0x10 (REQ-4).
 */
#include <stdint.h>
#include "xil_io.h"
#include "xil_printf.h"

#define UART_BASE   0xA0000000u        /* build_bd.tcl address map */
#define R_TXDATA    0x00u
#define R_RXDATA    0x04u
#define R_STATUS    0x08u
#define R_CTRL      0x0Cu
#define R_BAUD      0x10u

#define ST_TX_FULL     (1u << 0)
#define ST_TX_EMPTY    (1u << 1)
#define ST_RX_FULL     (1u << 2)
#define ST_RX_EMPTY    (1u << 3)
#define ST_TX_BUSY     (1u << 4)
#define ST_RX_OVERRUN  (1u << 5)
#define ST_FRAME_ERR   (1u << 6)
#define ST_PARITY_ERR  (1u << 7)
#define ST_ERRORS      (ST_RX_OVERRUN | ST_FRAME_ERR | ST_PARITY_ERR)

#define CTRL_TX_EN     (1u << 0)
#define CTRL_RX_EN     (1u << 1)

#define CLK_HZ         100000000u      /* pl_clk0 */
#define BAUD_RATE      115200u

static inline uint32_t rd(uint32_t off)              { return Xil_In32(UART_BASE + off); }
static inline void     wr(uint32_t off, uint32_t v)  { Xil_Out32(UART_BASE + off, v); }

static void uart_putc(char c)
{
    while (rd(R_STATUS) & ST_TX_FULL) { }            /* REQ-6: never write when full */
    wr(R_TXDATA, (uint8_t)c);
}

static void uart_puts(const char *s)
{
    while (*s) uart_putc(*s++);
}

static int uart_getc(void)                            /* -1 when nothing received */
{
    if (rd(R_STATUS) & ST_RX_EMPTY) return -1;       /* REQ-16: never read when empty */
    return (int)(rd(R_RXDATA) & 0xFFu);
}

int main(void)
{
    uint32_t div = (CLK_HZ + BAUD_RATE / 2) / BAUD_RATE;   /* 868 */

    /* xil_printf has no %u: values here are small, %d is exact */
    xil_printf("uart_apb test: base 0x%08x, STATUS after reset 0x%02x (expect 0x0a), BAUD %d\r\n",
               UART_BASE, rd(R_STATUS), (int)rd(R_BAUD));

    wr(R_BAUD, div);                                  /* >= 4, REQ-22 */
    wr(R_CTRL, CTRL_TX_EN | CTRL_RX_EN);              /* 8N1, interrupts off */
    xil_printf("CTRL 0x%02x BAUD %d -> %d baud 8N1\r\n", rd(R_CTRL), (int)rd(R_BAUD), (int)(CLK_HZ / rd(R_BAUD)));

    uart_puts("\r\nHello from uart_apb on ZCU104. Type to echo.\r\n");

    for (;;) {
        int c = uart_getc();
        if (c >= 0) {
            uart_putc((char)c);
            if (c == '\r') uart_putc('\n');
        }
        uint32_t st = rd(R_STATUS);
        if (st & ST_ERRORS) {
            xil_printf("rx error: STATUS 0x%02x (overrun/frame/parity), cleared\r\n", st);
            wr(R_STATUS, st & ST_ERRORS);             /* REQ-20 write-1-to-clear */
        }
    }
    return 0;
}
