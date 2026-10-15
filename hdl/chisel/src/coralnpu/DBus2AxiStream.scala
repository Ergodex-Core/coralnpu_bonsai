// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package coralnpu

import chisel3._
import chisel3.util._

import bus.AxiResponseType

// LSU external-bus bridge with stream prefetching on the read side.
//
// DBus2AxiV2 sends one single-beat AXI read per LSU request and waits for the
// data, so every external load pays the whole memory latency. This bridge
// keeps `streams` sequential read streams. A read that misses starts a stream
// at its row; the stream then keeps up to `depth` rows requested ahead with
// INCR bursts of up to `burst` beats, several bursts in flight on one AXI ID
// (so data returns in order). A row that has arrived is returned on the next
// cycle, which is the normal DBus read timing, so the in-order LSU stops
// waiting on DRAM for streaming loads.
//
// Ordering with writes: writes are unchanged from V2 (one at a time, wait for
// B). While a write is pending no read burst is issued, and a write drops any
// stream that holds the written row. `flush` (core reset or halt) drops all
// streams; bursts still in flight for a dropped stream are discarded by a
// per-stream generation tag. Rows below `ebusCachedBase` (CSRs, UART) are
// fetched one beat at a time and never kept.
class DBus2AxiStream(p: Parameters, id: Int) extends DBus2AxiV2(p, id) {
  val streams = p.ebusPrefetchStreams
  val depth   = p.ebusPrefetchDepth
  val burst   = p.ebusPrefetchBurst
  require(streams >= 2 && isPow2(depth) && isPow2(burst) && burst <= depth)
  require(burst * p.axi2DataBytes <= 4096) // aligned bursts never cross 4 KiB

  val rowShift = log2Ceil(p.axi2DataBytes)
  val rowBits  = p.axi2AddrBits - rowShift
  val cntBits  = log2Ceil(depth + 1)
  val genBits  = 4
  def slot(row: UInt): UInt = row(log2Ceil(depth) - 1, 0)
  // Rotate a depth-bit mask left by `sh`.
  def rotl(m: UInt, sh: UInt): UInt = (Cat(m, m) << sh)(2 * depth - 1, depth)

  // Per-stream state: rows [head, head + issued) have been requested and
  // filled(slot(row)) marks the ones that have arrived.
  val valid  = RegInit(VecInit(Seq.fill(streams)(false.B)))
  val single = Reg(Vec(streams, Bool()))   // uncached: one access, as in V2
  val sAddr  = Reg(Vec(streams, UInt(p.axi2AddrBits.W)))
  val sSize  = Reg(Vec(streams, UInt(3.W)))
  val head   = Reg(Vec(streams, UInt(rowBits.W)))
  val issued = RegInit(VecInit(Seq.fill(streams)(0.U(cntBits.W))))
  val filled = RegInit(VecInit(Seq.fill(streams)(0.U(depth.W))))
  val error  = Reg(Vec(streams, UInt(depth.W)))
  val gen    = RegInit(VecInit(Seq.fill(streams)(0.U(genBits.W))))
  val data   = Mem(streams * depth, UInt(p.axi2DataBits.W))
  def index(s: UInt, row: UInt): UInt = Cat(s, slot(row))

  // ---------------------------------------------------------------------------
  // LSU request: hit, wait for an issued row, or start a new stream.
  val reqRead = io.dbus.valid && !io.dbus.write
  val reqRow  = io.dbus.addr >> rowShift
  val rel     = VecInit(head.map(h => (reqRow - h)(rowBits - 1, 0)))
  val inWin   = VecInit((0 until streams).map(s => valid(s) && rel(s) < issued(s)))
  val hits    = VecInit((0 until streams).map(s => inWin(s) && filled(s)(slot(reqRow))))
  val owns    = VecInit((0 until streams).map { s =>
    valid(s) && Mux(single(s), rel(s) === 0.U, rel(s) <= issued(s))
  })
  val hit     = reqRead && hits.asUInt.orR
  val owned   = owns.asUInt.orR
  val sel     = Mux(hits.asUInt.orR, PriorityEncoder(hits), PriorityEncoder(owns))
  val advance = reqRead && owned // drop rows behind the request
  val alloc   = reqRead && !owned && !io.flush
  val rr      = RegInit(0.U(log2Ceil(streams).W))
  val victim  = Mux(valid.asUInt.andR, rr, PriorityEncoder(valid.map(!_)))
  rr := Mux(alloc, rr + 1.U, rr)

  io.dbus.rdata := RegEnable(data(index(sel, reqRow)), hit)
  io.dbus.ready := Mux(io.dbus.write, writeFinished, hit)
  io.fault.valid := io.dbus.valid && Mux(
    io.dbus.write,
    io.axi.write.resp.valid && (io.axi.write.resp.bits.resp =/= AxiResponseType.OKAY.asUInt),
    hit && error(sel)(slot(reqRow))
  )

  // ---------------------------------------------------------------------------
  // Prefetch: one burst per cycle, the waiting request's stream first.
  class Burst extends Bundle {
    val stream = UInt(log2Ceil(streams).W)
    val row    = UInt(rowBits.W)
    val gen    = UInt(genBits.W)
  }
  val bursts = Module(new Queue(new Burst, p.ebusPrefetchBursts))
  val next   = VecInit((0 until streams).map(s => head(s) + issued(s)))
  val room   = VecInit((0 until streams).map(s => Mux(single(s), 1.U, depth.U) - issued(s)))
  val beats  = VecInit((0 until streams).map { s =>
    val toEdge = if (burst == 1) 1.U else burst.U - next(s)(log2Ceil(burst) - 1, 0)
    Mux(toEdge < room(s), toEdge, room(s))
  })
  val want  = VecInit((0 until streams).map(s => valid(s) && room(s) =/= 0.U))
  val pick  = Mux(advance && want(sel), sel, PriorityEncoder(want))
  val write = io.dbus.valid && io.dbus.write
  io.axi.read.addr.valid := want.asUInt.orR && bursts.io.enq.ready && !write && !io.flush
  io.axi.read.addr.bits.defaults()
  io.axi.read.addr.bits.addr := Mux(single(pick), sAddr(pick), next(pick) << rowShift)
  io.axi.read.addr.bits.len  := beats(pick) - 1.U
  io.axi.read.addr.bits.size := Mux(single(pick), sSize(pick), rowShift.U)
  io.axi.read.addr.bits.prot := 2.U
  io.axi.read.addr.bits.id   := id.U
  val issue = io.axi.read.addr.fire
  bursts.io.enq.valid       := issue
  bursts.io.enq.bits.stream := pick
  bursts.io.enq.bits.row    := next(pick)
  bursts.io.enq.bits.gen    := gen(pick)

  // ---------------------------------------------------------------------------
  // Read data: beats return in issue order; keep those still wanted.
  val beat = RegInit(0.U(8.W))
  val rec  = bursts.io.deq.bits
  val row  = rec.row + beat
  val keep = io.axi.read.data.fire && valid(rec.stream) && gen(rec.stream) === rec.gen &&
    (row - head(rec.stream))(rowBits - 1, 0) < issued(rec.stream)
  io.axi.read.data.ready := true.B
  bursts.io.deq.ready    := io.axi.read.data.fire && io.axi.read.data.bits.last
  beat := Mux(io.axi.read.data.fire, Mux(io.axi.read.data.bits.last, 0.U, beat + 1.U), beat)
  when(keep) { data(index(rec.stream, row)) := io.axi.read.data.bits.data }

  // ---------------------------------------------------------------------------
  // Stream state update.
  for (s <- 0 until streams) {
    val adv  = advance && sel === s.U
    val drop = Mux(adv, rel(s)(cntBits - 1, 0), 0.U) // <= issued <= depth
    val lowMask = ((1.U((depth + 1).W) << drop) - 1.U)(depth - 1, 0)
    val dropMask = rotl(lowMask, slot(head(s)))
    val setMask = Mux(keep && rec.stream === s.U, UIntToOH(slot(row), depth), 0.U)
    val err = io.axi.read.data.bits.resp =/= AxiResponseType.OKAY.asUInt
    head(s)   := Mux(adv, reqRow, head(s))
    issued(s) := issued(s) - drop + Mux(issue && pick === s.U, beats(s), 0.U)
    filled(s) := (filled(s) | setMask) & ~dropMask
    error(s)  := Mux(err, error(s) | setMask, error(s) & ~setMask)
    // A single (uncached) row is used once.
    when((hit && sel === s.U && single(s)) || (write && valid(s) && rel(s) < issued(s)) ||
      io.flush) {
      valid(s) := false.B
    }
    when(alloc && victim === s.U) {
      valid(s)  := true.B
      single(s) := io.dbus.addr < p.ebusCachedBase.U
      sAddr(s)  := io.dbus.addr
      sSize(s)  := Ctz(io.dbus.size)
      head(s)   := reqRow
      issued(s) := 0.U
      filled(s) := 0.U
      gen(s)    := gen(s) + 1.U
    }
  }
}
