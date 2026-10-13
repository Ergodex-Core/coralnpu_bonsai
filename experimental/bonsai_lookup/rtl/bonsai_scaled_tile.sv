// Full-K M<=8,N<=32 scaled tile. Standalone tagged memory bus, not AXI.
// Layout: G slab32 weight groups, G contiguous M*128 activation groups,
// and G 32-byte activation-scale records (8 little-endian FP32 slots).
module bonsai_scaled_tile #(
    parameter integer ENGINES = 2,
    parameter integer LOOKUP  = 1
) (
    input wire clk,
    input wire rst_n,
    input wire job_valid,
    output wire job_ready,
    input wire [3:0] job_m,
    input wire [5:0] job_n,
    input wire [5:0] job_groups,
    input wire [31:0] job_tag,
    input wire [31:0] weight_base,
    input wire [31:0] activation_base,
    input wire [31:0] scale_base,
    output wire rd_valid,
    input wire rd_ready,
    output wire [31:0] rd_addr,
    output wire [3:0] rd_id,
    input wire rsp_valid,
    output wire rsp_ready,
    input wire [127:0] rsp_data,
    input wire [3:0] rsp_id,
    input wire rsp_error,
    output wire out_valid,
    input wire out_ready,
    output wire [31:0] out_value,
    output wire [3:0] out_token,
    output wire [5:0] out_row,
    output wire [31:0] out_tag,
    output wire out_error,
    output wire [4:0] out_fflags,
    output reg done,
    output reg job_error,
    output reg [31:0] cycles_total,
    output reg [31:0] bytes_read
);
  localparam IDLE=0, START=1, SCALE_REQ=2, SCALE_RSP=3, TILE_START=4, TILE_RUN=5, DRAIN=6;
  reg [2:0] state;
  reg [3:0] m;
  reg [5:0] n, groups, group_id;
  reg [31:0] wb, ab, sb, tag;
  reg scale_beat, scale_fault;
  reg [255:0] activation_scales;
  reg [  3:0] lane;
  wire tile_ready, tile_rd_valid, tile_rsp_ready, tile_valid, tile_done, tile_error;
  wire [31:0] tile_addr;
  wire [3:0] tile_id, tile_token;
  wire [5:0] tile_row;
  wire [ENGINES*32-1:0] dots;
  wire [ENGINES*16-1:0] weight_scales;
  wire [ENGINES-1:0] masks, errors;
  wire reducer_ready, reducer_start_ready, reducer_valid, reducer_last, reducer_done, reducer_error;
  wire [5:0] unused_group;
  wire tile_accept = tile_valid && reducer_ready;
  wire last_lane = (int'(lane) + 1 >= ENGINES || int'(tile_row) + int'(lane) + 1 >= int'(n));
  wire [63:0] weight_end = {32'b0, weight_base} + 64'(job_groups) * 1088;
  wire [63:0] activation_end = {32'b0, activation_base} + 64'(job_groups) * 64'(job_m) * 128;
  wire [63:0] scale_end = {32'b0, scale_base} + 64'(job_groups) * 32;
  assign job_ready = state == IDLE;
  assign rd_valid = state == SCALE_REQ || (state == TILE_RUN && tile_rd_valid);
  assign rd_addr = state == SCALE_REQ ? sb + 32'(scale_beat) * 16 : tile_addr;
  assign rd_id = state == SCALE_REQ ? {3'b0, scale_beat} : tile_id;
  assign rsp_ready = state == SCALE_RSP || (state == TILE_RUN && tile_rsp_ready);
  assign out_valid = reducer_valid && reducer_last;

  bonsai_matmul_tile #(
      .ENGINES(ENGINES),
      .LOOKUP (LOOKUP)
  ) tile (
      .clk(clk),
      .rst_n(rst_n),
      .job_valid(state == TILE_START),
      .job_ready(tile_ready),
      .job_m(m),
      .job_n(n),
      .job_lanes(8'd128),
      .job_tag(tag),
      .weight_base(wb),
      .activation_base(ab),
      .rd_valid(tile_rd_valid),
      .rd_ready(rd_ready && state == TILE_RUN),
      .rd_addr(tile_addr),
      .rd_id(tile_id),
      .rsp_valid(rsp_valid && state == TILE_RUN),
      .rsp_ready(tile_rsp_ready),
      .rsp_data(rsp_data),
      .rsp_id(rsp_id),
      .rsp_error(rsp_error),
      .out_valid(tile_valid),
      .out_ready(reducer_ready && last_lane),
      .out_dots(dots),
      .out_scales(weight_scales),
      .out_mask(masks),
      .out_error(errors),
      .out_tag(),
      .out_token(tile_token),
      .out_row_base(tile_row),
      .done(tile_done),
      .job_error(tile_error),
      .cycles_total(),
      .cycles_load(),
      .bytes_read()
  );
  bonsai_group_reduce reducer (
      .clk(clk),
      .rst_n(rst_n),
      .start_valid(state == START),
      .start_ready(reducer_start_ready),
      .start_m(m),
      .start_n(n),
      .start_groups(groups),
      .start_tag(tag),
      .in_valid(tile_valid),
      .in_ready(reducer_ready),
      .in_dot(dots[lane*32+:32]),
      .in_weight_scale(weight_scales[lane*16+:16]),
      .in_activation_scale(activation_scales[tile_token*32+:32]),
      .in_token(tile_token),
      .in_row(tile_row + 6'(lane)),
      .in_group(group_id),
      .in_error(errors[lane[$clog2(
          ENGINES
      )-1:0]] || !masks[lane[$clog2(
          ENGINES
      )-1:0]] || scale_fault),
      .out_valid(reducer_valid),
      .out_ready(!reducer_last || out_ready),
      .out_value(out_value),
      .out_token(out_token),
      .out_row(out_row),
      .out_group(unused_group),
      .out_last(reducer_last),
      .out_error(out_error),
      .out_fflags(out_fflags),
      .out_tag(out_tag),
      .done(reducer_done),
      .job_error(reducer_error)
  );
  always @(posedge clk) begin
    if (!rst_n) begin
      state <= IDLE;
      done <= 0;
      job_error <= 0;
      cycles_total <= 0;
      bytes_read <= 0;
      m <= 0;
      n <= 0;
      groups <= 0;
      group_id <= 0;
      wb <= 0;
      ab <= 0;
      sb <= 0;
      tag <= 0;
      scale_beat <= 0;
      scale_fault <= 0;
      activation_scales <= 0;
      lane <= 0;
    end else begin
      done <= 0;
      if (state != IDLE) cycles_total <= cycles_total + 1;
      if (rsp_valid && rsp_ready) bytes_read <= bytes_read + 16;
      if (tile_accept) begin
        if (last_lane) lane <= 0;
        else lane <= lane + 1;
      end
      case (state)
        IDLE:
        if (job_valid) begin
          cycles_total <= 0;
          bytes_read <= 0;
          job_error <= 0;
          group_id <= 0;
          scale_beat <= 0;
          scale_fault <= 0;
          lane <= 0;
          if(job_m==0 || job_m>8 || job_n==0 || job_n>32 || job_groups==0 || job_groups>48 ||
              weight_base[3:0]!=0 || activation_base[3:0]!=0 || scale_base[3:0]!=0 ||
              weight_end>64'h100000000 || activation_end>64'hfffffc00+64'(job_m)*128 || scale_end>64'h100000000) begin
            done <= 1;
            job_error <= 1;
          end else begin
            m <= job_m;
            n <= job_n;
            groups <= job_groups;
            tag <= job_tag;
            wb <= weight_base;
            ab <= activation_base;
            sb <= scale_base;
            state <= START;
          end
        end
        START: if (reducer_start_ready) state <= SCALE_REQ;
        SCALE_REQ: if (rd_ready) state <= SCALE_RSP;
        SCALE_RSP:
        if (rsp_valid) begin
          if (rsp_error || rsp_id != {3'b0, scale_beat}) scale_fault <= 1;
          activation_scales[scale_beat*128+:128] <= rsp_data;
          if (scale_beat) state <= TILE_START;
          else begin
            scale_beat <= 1;
            state <= SCALE_REQ;
          end
        end
        TILE_START: if (tile_ready) state <= TILE_RUN;
        TILE_RUN:
        if (tile_done) begin
          if (tile_error) job_error <= 1;
          if (group_id == groups - 1) state <= DRAIN;
          else begin
            group_id <= group_id + 1;
            wb <= wb + 1088;
            ab <= ab + 32'(m) * 128;
            sb <= sb + 32;
            scale_beat <= 0;
            state <= SCALE_REQ;
          end
        end
        DRAIN:
        if (reducer_done) begin
          job_error <= job_error || reducer_error || scale_fault;
          done <= 1;
          state <= IDLE;
        end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
