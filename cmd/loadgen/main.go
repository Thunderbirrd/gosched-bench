// Command loadgen — генератор LC-нагрузки.
//
// Режим -mode=open (по умолчанию, основной для статьи 1): открытый цикл.
// Моменты отправки — пуассоновское расписание, построенное заранее.
// Задержка R считается от ЗАПЛАНИРОВАННОГО момента отправки, поэтому
// коррекция coordinated omission заложена в схему измерения.
// Отставание фактической отправки от расписания (lag) пишется отдельно:
// если его P99,9 больше -lag-max, прогон помечается невалидным.
//
// Режим -mode=closed (для статьи 2, демонстрация coordinated omission):
// закрытый цикл в стиле wrk. Каждое из -conns соединений отправляет
// следующий запрос только после ответа на предыдущий, стараясь держать
// ту же интенсивность по тому же расписанию. Если ответ опоздал, следующий
// запрос уходит сразу, но позже запланированного. Пишутся две гистограммы:
//
//	latency          — от ФАКТИЧЕСКОЙ отправки (так считают закрытые генераторы);
//	latency_intended — от запланированного момента (то, что увидел бы клиент
//	                   открытой системы; поправка на coordinated omission).
//
// Расписание и времена обслуживания те же, что в открытом режиме при том же
// -seed, поэтому прогоны двух режимов сравнимы запрос в запрос.
package main

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"gosched-bench/internal/dist"
	"gosched-bench/internal/hist"
	"gosched-bench/internal/proto"
)

var (
	addr      = flag.String("addr", "127.0.0.1:7070", "адрес сервера")
	rate      = flag.Float64("rate", 1000, "интенсивность LC, запросов/с")
	dur       = flag.Duration("dur", 30*time.Second, "длительность окна измерения")
	warmup    = flag.Duration("warmup", 5*time.Second, "прогрев перед окном (в статистику не идёт)")
	distSpec  = flag.String("dist", "exp:50us", "распределение времени обслуживания: const:, exp:, bimodal:short,long,p")
	alloc     = flag.Int("alloc", 0, "байт мусора на запрос в обработчике (фактор a_LC)")
	conns     = flag.Int("conns", 32, "число TCP-соединений")
	seed      = flag.Int64("seed", 1, "seed расписания и времени обслуживания")
	outDir    = flag.String("out", ".", "каталог для summary.json, latency_hist.json, samples.csv")
	sample    = flag.Float64("sample", 0.01, "доля запросов окна, записываемых построчно в samples.csv")
	spinAhead = flag.Duration("spin-ahead", 100*time.Microsecond, "последний отрезок ожидания крутить в цикле, а не спать")
	lagMax    = flag.Duration("lag-max", 50*time.Microsecond, "порог P99,9 отставания отправки для валидности прогона")
	drain     = flag.Duration("drain", 2*time.Second, "сколько ждать ответы после последней отправки")
	mode      = flag.String("mode", "open", "open — открытый цикл; closed — закрытый (запрос после ответа)")
	raw       = flag.Bool("raw", false, "писать все задержки окна в raw_latency.bin (int64 нс, little-endian; статья 2)")
)

type row struct {
	id                                 int
	schedNs, sendNs, recvNs, serviceNs int64 // часы клиента, от base
	tRead, tStart, tEnd                int64 // часы сервера
}

// reader — состояние одного читателя (свои гистограммы, без блокировок).
// rIntended и lag заполняются только в закрытом режиме.
type reader struct {
	r, rIntended, handlerWait, serverTotal, lag *hist.Hist
	rows                                        []row
	raw, rawIntended                            []int64 // только при -raw
}

// add записывает одну задержку окна: в гистограмму и, при -raw, в сырой массив.
func (rd *reader) add(r, intended int64, closed bool) {
	rd.r.Record(r)
	if *raw {
		rd.raw = append(rd.raw, r)
	}
	if closed {
		rd.rIntended.Record(intended)
		if *raw {
			rd.rawIntended = append(rd.rawIntended, intended)
		}
	}
}

func newReader() *reader {
	return &reader{r: hist.New(), rIntended: hist.New(), handlerWait: hist.New(),
		serverTotal: hist.New(), lag: hist.New()}
}

func main() {
	flag.Parse()
	log.SetFlags(log.Lmicroseconds)
	if *mode != "open" && *mode != "closed" {
		log.Fatalf("-mode: ожидается open или closed, получено %q", *mode)
	}
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		log.Fatal(err)
	}
	d, err := dist.Parse(*distSpec)
	if err != nil {
		log.Fatal(err)
	}

	rnd := rand.New(rand.NewSource(*seed))
	sched := dist.PoissonSchedule(rnd, *rate, *warmup+*dur)
	service := make([]int64, len(sched))
	for i := range service {
		service[i] = int64(d.Sample(rnd))
	}
	sent := make([]atomic.Int64, len(sched)) // фактический момент отправки, нс от base
	stride := 0
	if *sample > 0 {
		stride = int(1 / *sample)
	}
	log.Printf("режим %s; расписание: %d запросов, rate=%.0f/с, E[S]=%v, warmup=%v, dur=%v",
		*mode, len(sched), *rate, d.Mean(), *warmup, *dur)

	cs := make([]net.Conn, *conns)
	ws := make([]*bufio.Writer, *conns)
	for i := range cs {
		c, err := dialRetry(*addr, 15*time.Second)
		if err != nil {
			log.Fatalf("dial: %v", err)
		}
		cs[i] = c
		ws[i] = bufio.NewWriterSize(c, proto.ReqSize)
	}

	base := time.Now().Add(100 * time.Millisecond)
	fmt.Println("START") // run.py отсчитывает прогрев от этой строки
	var received atomic.Int64
	readers := make([]*reader, *conns)
	var wg sync.WaitGroup
	lag := hist.New()

	if *mode == "closed" {
		runClosed(cs, ws, readers, sched, service, base, stride, &received, &wg)
		lag = nil // в закрытом режиме отставание — часть измеряемого эффекта, см. lag в reader
	} else {
		runOpen(cs, ws, readers, sched, service, sent, base, stride, &received, &wg, lag)
	}
	finish(cs, readers, sched, d, lag, &received, &wg)
}

// runOpen — открытый цикл: читатели по соединениям и один отправитель по расписанию.
func runOpen(cs []net.Conn, ws []*bufio.Writer, readers []*reader, sched []time.Duration, service []int64,
	sent []atomic.Int64, base time.Time, stride int, received *atomic.Int64, wg *sync.WaitGroup, lag *hist.Hist) {
	warmNs := warmup.Nanoseconds()
	for i, c := range cs {
		rd := newReader()
		readers[i] = rd
		wg.Add(1)
		go func(c net.Conn) {
			defer wg.Done()
			br := bufio.NewReaderSize(c, 64<<10)
			var buf [proto.RespSize]byte
			var resp proto.Response
			for {
				if err := proto.ReadResponse(br, buf[:], &resp); err != nil {
					return
				}
				now := time.Since(base).Nanoseconds()
				id := int(resp.ID)
				received.Add(1)
				s := int64(sched[id])
				if s < warmNs {
					continue
				}
				rd.add(now-s, 0, false)
				rd.handlerWait.Record(resp.TStart - resp.TRead)
				rd.serverTotal.Record(resp.TEnd - resp.TRead)
				if stride > 0 && id%stride == 0 {
					rd.rows = append(rd.rows, row{id, s, sent[id].Load(), now, service[id], resp.TRead, resp.TStart, resp.TEnd})
				}
			}
		}(c)
	}

	// Отправитель: одна горутина, ждёт до запланированного момента
	// (sleep, затем последние spin-ahead — активное ожидание).
	var buf [proto.ReqSize]byte
	for i, off := range sched {
		target := base.Add(off)
		waitUntil(target)
		req := proto.Request{ID: uint64(i), ServiceNs: uint64(service[i]), AllocBytes: uint32(*alloc)}
		req.Encode(buf[:])
		w := ws[i%len(ws)]
		w.Write(buf[:])
		if err := w.Flush(); err != nil {
			log.Fatalf("write: %v", err)
		}
		now := time.Since(base).Nanoseconds()
		sent[i].Store(now)
		if int64(off) >= warmNs {
			lag.Record(now - int64(off))
		}
	}
	lastSend := time.Now()

	for received.Load() < int64(len(sched)) && time.Since(lastSend) < *drain {
		time.Sleep(10 * time.Millisecond)
	}
}

// runClosed — закрытый цикл: соединение отправляет следующий запрос только после
// ответа на предыдущий. Соединению c достаются запросы c, c+conns, c+2·conns, …
// общего расписания. Как и в открытом режиме, моменты отправки отсчитывает одна
// горутина; если соединение ещё ждёт ответа, запрос встаёт в его очередь и уходит
// сразу после ответа (из горутины-читателя).
//
// Прежний вариант (у каждого соединения горутина, активно ждущая свой момент с
// Gosched) не давал планировщику генератора вовремя опрашивать сеть: медиана
// задержки получалась 0,6–0,9 мс вместо ~60 мкс.
func runClosed(cs []net.Conn, ws []*bufio.Writer, readers []*reader, sched []time.Duration, service []int64,
	base time.Time, stride int, received *atomic.Int64, wg *sync.WaitGroup) {
	warmNs := warmup.Nanoseconds()
	type connState struct {
		mu      sync.Mutex
		busy    bool  // запрос отправлен, ответа ещё нет
		sendNs  int64 // момент отправки запроса, на который ждём ответ
		pending []int // запросы, чьё время пришло, пока соединение было занято
	}
	st := make([]connState, len(cs))
	send := func(ci, i int) { // вызывать под st[ci].mu
		var buf [proto.ReqSize]byte
		st[ci].busy = true
		st[ci].sendNs = time.Since(base).Nanoseconds()
		r := proto.Request{ID: uint64(i), ServiceNs: uint64(service[i]), AllocBytes: uint32(*alloc)}
		r.Encode(buf[:])
		ws[ci].Write(buf[:])
		if err := ws[ci].Flush(); err != nil {
			log.Fatalf("write: %v", err)
		}
	}
	for ci := range cs {
		rd := newReader()
		readers[ci] = rd
		wg.Add(1)
		go func(ci int) {
			defer wg.Done()
			br := bufio.NewReaderSize(cs[ci], proto.RespSize)
			var rb [proto.RespSize]byte
			var resp proto.Response
			c := &st[ci]
			for {
				if err := proto.ReadResponse(br, rb[:], &resp); err != nil {
					return
				}
				now := time.Since(base).Nanoseconds()
				c.mu.Lock()
				sendNs := c.sendNs
				if len(c.pending) > 0 {
					next := c.pending[0]
					c.pending = c.pending[1:]
					send(ci, next)
				} else {
					c.busy = false
				}
				c.mu.Unlock()
				received.Add(1)
				i := int(resp.ID)
				s := int64(sched[i])
				if s < warmNs {
					continue
				}
				rd.add(now-sendNs, now-s, true)
				rd.lag.Record(sendNs - s)
				rd.handlerWait.Record(resp.TStart - resp.TRead)
				rd.serverTotal.Record(resp.TEnd - resp.TRead)
				if stride > 0 && i%stride == 0 {
					rd.rows = append(rd.rows, row{i, s, sendNs, now, service[i], resp.TRead, resp.TStart, resp.TEnd})
				}
			}
		}(ci)
	}
	for i, off := range sched {
		waitUntil(base.Add(off))
		ci := i % len(cs)
		c := &st[ci]
		c.mu.Lock()
		if c.busy {
			c.pending = append(c.pending, i)
		} else {
			send(ci, i)
		}
		c.mu.Unlock()
	}
	lastSend := time.Now()
	for received.Load() < int64(len(sched)) && time.Since(lastSend) < *drain {
		time.Sleep(10 * time.Millisecond)
	}
}

// waitUntil ждёт до target: sleep, затем последние spin-ahead — активное ожидание.
func waitUntil(target time.Time) {
	if w := time.Until(target); w > *spinAhead {
		time.Sleep(w - *spinAhead)
	}
	for time.Now().Before(target) {
	}
}

func finish(cs []net.Conn, readers []*reader, sched []time.Duration, d dist.Dist, lag *hist.Hist,
	received *atomic.Int64, wg *sync.WaitGroup) {
	for _, c := range cs {
		c.Close()
	}
	wg.Wait()

	all := newReader()
	for _, rd := range readers {
		all.r.Merge(rd.r)
		all.rIntended.Merge(rd.rIntended)
		all.handlerWait.Merge(rd.handlerWait)
		all.serverTotal.Merge(rd.serverTotal)
		all.lag.Merge(rd.lag)
		all.rows = append(all.rows, rd.rows...)
		all.raw = append(all.raw, rd.raw...)
		all.rawIntended = append(all.rawIntended, rd.rawIntended...)
	}
	if *raw {
		writeRaw(filepath.Join(*outDir, "raw_latency.bin"), all.raw)
		if *mode == "closed" {
			writeRaw(filepath.Join(*outDir, "raw_latency_intended.bin"), all.rawIntended)
		}
	}

	lost := int64(len(sched)) - received.Load()
	window := all.r.Summary().Count
	summary := map[string]any{
		"args":            os.Args[1:],
		"mode":            *mode,
		"rate":            *rate,
		"achieved_rate":   float64(window) / dur.Seconds(), // ответов в окне за секунду
		"dist":            *distSpec,
		"mean_service_ns": d.Mean().Nanoseconds(),
		"conns":           *conns,
		"scheduled":       len(sched),
		"received":        received.Load(),
		"lost":            lost,
		"handler_wait":    all.handlerWait.Summary(), // TStart-TRead на сервере
		"server_total":    all.serverTotal.Summary(), // TEnd-TRead на сервере
	}
	valid := true
	if *mode == "open" {
		lagSum := lag.Summary()
		valid = lagSum.P999 <= lagMax.Nanoseconds()
		summary["latency"] = all.r.Summary() // R от запланированного момента
		summary["send_lag"] = lagSum         // отставание генератора
	} else {
		// Отставание отправки здесь не дефект генератора, а суть закрытого цикла:
		// на валидность не влияет, но пишется — это величина coordinated omission.
		summary["latency"] = all.r.Summary()                  // от фактической отправки
		summary["latency_intended"] = all.rIntended.Summary() // от запланированного момента
		summary["send_lag"] = all.lag.Summary()
		writeJSON(filepath.Join(*outDir, "latency_intended_hist.json"), all.rIntended)
	}
	summary["valid"] = valid && lost == 0
	summary["invalid_reasons"] = reasons(valid, lost)
	writeJSON(filepath.Join(*outDir, "summary.json"), summary)
	writeJSON(filepath.Join(*outDir, "latency_hist.json"), all.r)
	writeRows(filepath.Join(*outDir, "samples.csv"), all.rows)

	s := all.r.Summary()
	log.Printf("R: p50=%v p99=%v p99.9=%v max=%v; lost=%d; valid=%v",
		time.Duration(s.P50), time.Duration(s.P99), time.Duration(s.P999), time.Duration(s.Max),
		lost, summary["valid"])
	if *mode == "closed" {
		si := all.rIntended.Summary()
		log.Printf("R от плана: p50=%v p99=%v p99.9=%v; достигнуто %.0f/с из %.0f/с",
			time.Duration(si.P50), time.Duration(si.P99), time.Duration(si.P999),
			summary["achieved_rate"], *rate)
	}
}

// dialRetry повторяет подключение: в docker compose сервер может ещё
// калиброваться, когда генератор уже стартовал.
func dialRetry(addr string, total time.Duration) (net.Conn, error) {
	deadline := time.Now().Add(total)
	for {
		c, err := net.Dial("tcp", addr)
		if err == nil || time.Now().After(deadline) {
			return c, err
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func reasons(lagOK bool, lost int64) []string {
	var r []string
	if !lagOK {
		r = append(r, "send_lag_p999_above_lag_max")
	}
	if lost > 0 {
		r = append(r, "lost_responses")
	}
	return r
}

func writeJSON(path string, v any) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		log.Fatalf("json: %v", err)
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		log.Fatalf("write %s: %v", path, err)
	}
}

// writeRaw пишет задержки как int64 little-endian подряд, без заголовка.
// Порядок — по соединениям, не по времени; для оценок квантилей это не важно.
// Чтение в Python: array.array("q", open(path, "rb").read()).
func writeRaw(path string, xs []int64) {
	b := make([]byte, 8*len(xs))
	for i, x := range xs {
		binary.LittleEndian.PutUint64(b[8*i:], uint64(x))
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		log.Fatalf("write %s: %v", path, err)
	}
}

func writeRows(path string, rows []row) {
	f, err := os.Create(path)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	defer w.Flush()
	fmt.Fprintln(w, "id,sched_ns,send_ns,recv_ns,service_ns,srv_t_read,srv_t_start,srv_t_end")
	for _, r := range rows {
		fmt.Fprintf(w, "%d,%d,%d,%d,%d,%d,%d,%d\n", r.id, r.schedNs, r.sendNs, r.recvNs, r.serviceNs, r.tRead, r.tStart, r.tEnd)
	}
}
