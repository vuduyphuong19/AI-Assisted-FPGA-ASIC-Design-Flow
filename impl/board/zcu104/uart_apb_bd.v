`default_nettype none
// uart_apb_bd.v : Block Design wrapper for the signed-off rtl/uart_apb.v (board integration for
// ZCU104; manual, not part of the mae flow). It adds no logic. It only:
//   - declares the Vivado interfaces (APB slave S_APB, clock, active-low reset, level interrupt),
//     so the IP integrator connects it to axi_apb_bridge as one interface;
//   - takes the bridge's 32-bit PADDR and passes paddr[4:0] (ADDR_W = 5). Addresses base+0x20 ..
//     base+0xFFF alias the 0x00..0x1F map (spec ADDR_W = 5), so software uses offsets 0x00..0x10 only.
module uart_apb_bd #(
    parameter DIV_RESET = 868                  //! BAUD after reset; 868 = 115200 baud at 100 MHz
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_APB, ASSOCIATED_RESET rst_n" *)
    input  wire        clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rst_n RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        rst_n,

    // bits [31:5] are unused on purpose: the block decodes paddr[4:0] (spec ADDR_W = 5)
    /* verilator lint_off UNUSED */
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PADDR" *)
    input  wire [31:0] s_apb_paddr,
    /* verilator lint_on UNUSED */
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PSEL" *)
    input  wire        s_apb_psel,
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PENABLE" *)
    input  wire        s_apb_penable,
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PWRITE" *)
    input  wire        s_apb_pwrite,
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PWDATA" *)
    input  wire [31:0] s_apb_pwdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PRDATA" *)
    output wire [31:0] s_apb_prdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PREADY" *)
    output wire        s_apb_pready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:apb:1.0 S_APB PSLVERR" *)
    output wire        s_apb_pslverr,

    (* X_INTERFACE_INFO = "xilinx.com:signal:interrupt:1.0 irq INTERRUPT" *)
    (* X_INTERFACE_PARAMETER = "SENSITIVITY LEVEL_HIGH" *)
    output wire        irq,
    input  wire        rxd,                    //! UART in, asynchronous (board pin)
    output wire        txd                     //! UART out (board pin)
);

    uart_apb #(
        .FIFO_DEPTH (4),
        .ADDR_W     (5),
        .DIV_RESET  (DIV_RESET),
        .DIV_MIN    (4)
    ) u_uart (
        .clk     (clk),
        .rst_n   (rst_n),
        .psel    (s_apb_psel),
        .penable (s_apb_penable),
        .pwrite  (s_apb_pwrite),
        .paddr   (s_apb_paddr[4:0]),
        .pwdata  (s_apb_pwdata),
        .prdata  (s_apb_prdata),
        .pready  (s_apb_pready),
        .pslverr (s_apb_pslverr),
        .rxd     (rxd),
        .txd     (txd),
        .irq     (irq)
    );

endmodule

`default_nettype wire
