// AXI burst/width adapter for the bounded DDR aperture. One source burst at a
// time; each source beat becomes one aligned 64-byte DDR access. Narrow reads
// return the corresponding source-bus lanes, not a right-justified value.
module coral_ddr_frontend #(
    parameter integer DATA_WIDTH = 128,
    parameter integer ADDR_WIDTH = 32,
    parameter integer ID_WIDTH = 6,
    parameter logic [63:0] BASE_ADDR = 64'h20000000,
    parameter logic [63:0] APERTURE_BYTES = 64'h80000000
) (
    input wire clk,
    rst_n,
    input wire [ADDR_WIDTH-1:0] s_awaddr,
    input wire [ID_WIDTH-1:0] s_awid,
    input wire [7:0] s_awlen,
    input wire [2:0] s_awsize,
    input wire [1:0] s_awburst,
    input wire s_awlock,
    s_awvalid,
    output wire s_awready,
    input wire [DATA_WIDTH-1:0] s_wdata,
    input wire [DATA_WIDTH/8-1:0] s_wstrb,
    input wire s_wlast,
    s_wvalid,
    output wire s_wready,
    output wire [ID_WIDTH-1:0] s_bid,
    output wire [1:0] s_bresp,
    output wire s_bvalid,
    input wire s_bready,
    input wire [ADDR_WIDTH-1:0] s_araddr,
    input wire [ID_WIDTH-1:0] s_arid,
    input wire [7:0] s_arlen,
    input wire [2:0] s_arsize,
    input wire [1:0] s_arburst,
    input wire s_arlock,
    s_arvalid,
    output wire s_arready,
    output wire [DATA_WIDTH-1:0] s_rdata,
    output wire [ID_WIDTH-1:0] s_rid,
    output wire [1:0] s_rresp,
    output wire s_rlast,
    s_rvalid,
    input wire s_rready,
    output wire req_valid,
    input wire req_ready,
    output wire req_write,
    output wire [31:0] req_addr,
    output logic [511:0] req_wdata,
    output logic [63:0] req_wstrb,
    input wire rsp_valid,
    output wire rsp_ready,
    input wire [511:0] rsp_rdata,
    input wire [1:0] rsp_resp
);
  localparam integer BUS_BYTES = DATA_WIDTH / 8;
  localparam integer BUS_SIZE = $clog2(BUS_BYTES);
  localparam [2:0] IDLE=0, WRITE_DATA=1, REQUEST=2, RESPONSE=3,
                   READ_OUT=4, WRITE_OUT=5, WRITE_DRAIN=6;
  logic [2:0] state;
  logic prefer_write, write_q;
  logic [ADDR_WIDTH-1:0] addr_q;
  logic [ID_WIDTH-1:0] id_q;
  logic [7:0] left_q;
  logic [2:0] size_q;
  logic [1:0] burst_q, resp_q;
  logic [DATA_WIDTH-1:0] data_q;
  logic [BUS_BYTES-1:0] legal_strobes;
  integer lane_shift;

  function automatic logic bad_address(input logic [ADDR_WIDTH-1:0] addr, input logic [7:0] len,
                                       input logic [2:0] size, input logic [1:0] burst,
                                       input logic locked);
    logic [64:0] first_byte, final_byte, beat_bytes, offset;
    begin
      first_byte = {1'b0, 64'(addr)};
      beat_bytes = 65'd1 << size;
      offset = first_byte - {1'b0, BASE_ADDR};
      final_byte = first_byte + ((burst == 2'b01 ? (65'(len) + 1) : 65'd1) << size) - 1;
      bad_address = locked || size > 3'(BUS_SIZE) || burst > 2'b01 ||
          (burst == 2'b00 && len > 15) ||
          ((first_byte & (beat_bytes-1)) != 0) ||
          offset[64] ||
          final_byte >= ({1'b0,BASE_ADDR}+{1'b0,APERTURE_BYTES}) ||
          final_byte[64] || (first_byte[64:12] != final_byte[64:12]);
    end
  endfunction

  assign s_awready = rst_n && state == IDLE && (!s_arvalid || prefer_write);
  assign s_arready = rst_n && state == IDLE && (!s_awvalid || !prefer_write);
  assign s_wready = rst_n && (state == WRITE_DATA || state == WRITE_DRAIN);
  assign s_bid = id_q;
  assign s_bresp = resp_q;
  assign s_bvalid = state == WRITE_OUT;
  assign s_rid = id_q;
  assign s_rdata = data_q;
  assign s_rresp = resp_q;
  assign s_rlast = left_q == 0;
  assign s_rvalid = state == READ_OUT;
  assign req_valid = state == REQUEST;
  assign req_write = write_q;
  assign req_addr = (32'(addr_q) - BASE_ADDR[31:0]) & 32'hffffffc0;
  assign rsp_ready = state == RESPONSE;

  always_comb begin
    lane_shift = (32'(addr_q) & (64 - BUS_BYTES)) * 8;
    for (integer b = 0; b < BUS_BYTES; b = b + 1)
    legal_strobes[b] = b >= (32'(addr_q) & (BUS_BYTES-1)) &&
                        b < ((32'(addr_q) & (BUS_BYTES-1)) + (1 << size_q));
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE;
      prefer_write <= 0;
      write_q <= 0;
      addr_q <= 0;
      id_q <= 0;
      left_q <= 0;
      size_q <= 0;
      burst_q <= 0;
      resp_q <= 0;
      data_q <= 0;
      req_wdata <= 0;
      req_wstrb <= 0;
    end else
      case (state)
        IDLE: begin
          if (s_awvalid && s_awready) begin
            write_q <= 1;
            addr_q <= s_awaddr;
            id_q <= s_awid;
            left_q <= s_awlen;
            size_q <= s_awsize;
            burst_q <= s_awburst;
            resp_q <= bad_address(s_awaddr, s_awlen, s_awsize, s_awburst, s_awlock) ? 2'b11 : 2'b00;
            state <= WRITE_DATA;
          end else if (s_arvalid && s_arready) begin
            write_q <= 0;
            addr_q <= s_araddr;
            id_q <= s_arid;
            left_q <= s_arlen;
            size_q <= s_arsize;
            burst_q <= s_arburst;
            data_q <= 0;
            resp_q <= bad_address(s_araddr, s_arlen, s_arsize, s_arburst, s_arlock) ? 2'b11 : 2'b00;
            state <= bad_address(
                s_araddr, s_arlen, s_arsize, s_arburst, s_arlock
            ) ? READ_OUT : REQUEST;
          end
        end
        WRITE_DATA:
        if (s_wvalid && s_wready) begin
          if (s_wlast != (left_q == 0)) begin
            // Malformed WLAST: never forward the offending beat. Early LAST
            // ends with SLVERR; missing LAST drains until LAST before responding.
            resp_q <= resp_q | 2'b10;
            state  <= s_wlast ? WRITE_OUT : WRITE_DRAIN;
          end else if (resp_q != 0 || (s_wstrb & ~legal_strobes) != 0) begin
            if ((s_wstrb & ~legal_strobes) != 0) resp_q <= resp_q | 2'b10;
            if (left_q == 0) state <= WRITE_OUT;
            else begin
              left_q <= left_q - 1'b1;
              if (burst_q == 1) addr_q <= addr_q + (ADDR_WIDTH'(1) << size_q);
            end
          end else begin
            req_wdata <= 512'(s_wdata) << lane_shift;
            req_wstrb <= 64'(s_wstrb) << (lane_shift / 8);
            state <= REQUEST;
          end
        end
        WRITE_DRAIN: if (s_wvalid && s_wready && s_wlast) state <= WRITE_OUT;
        REQUEST: if (req_valid && req_ready) state <= RESPONSE;
        RESPONSE:
        if (rsp_valid && rsp_ready) begin
          if (write_q) begin
            resp_q <= resp_q | rsp_resp;
            if (left_q == 0) state <= WRITE_OUT;
            else begin
              left_q <= left_q - 1'b1;
              if (burst_q == 1) addr_q <= addr_q + (ADDR_WIDTH'(1) << size_q);
              state <= WRITE_DATA;
            end
          end else begin
            data_q <= DATA_WIDTH'(rsp_rdata >> lane_shift);
            resp_q <= rsp_resp;
            state  <= READ_OUT;
          end
        end
        READ_OUT:
        if (s_rvalid && s_rready) begin
          if (left_q == 0) begin
            state <= IDLE;
            prefer_write <= 1;
          end else begin
            left_q <= left_q - 1'b1;
            if (burst_q == 1) addr_q <= addr_q + (ADDR_WIDTH'(1) << size_q);
            // Invalid address bursts remain DECERR with zero data throughout.
            // Downstream faults are likewise terminal for the remaining burst.
            state <= resp_q == 0 ? REQUEST : READ_OUT;
            if (resp_q != 0) data_q <= 0;
          end
        end
        WRITE_OUT:
        if (s_bvalid && s_bready) begin
          state <= IDLE;
          prefer_write <= 0;
        end
        default: state <= IDLE;
      endcase
  end
endmodule
