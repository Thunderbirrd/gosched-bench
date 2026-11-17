// Package proto описывает кадры обмена между loadgen и server.
// Кадры фиксированной длины, little-endian, без внешних зависимостей.
package proto

import (
	"encoding/binary"
	"io"
)

const (
	ReqSize  = 24
	RespSize = 32
)

// Request клиент сам задаёт время обслуживания и объём аллокаций,
// поэтому распределение S_LC полностью под контролем генератора.
type Request struct {
	ID         uint64
	ServiceNs  uint64 // сколько обработчик крутит CPU
	AllocBytes uint32 // сколько байт мусора аллоцировать в обработчике
}

// Response серверные метки времени (UnixNano по часам сервера).
// TRead  — кадр прочитан из сокета;
// TStart — горутина-обработчик начала работу (TStart-TRead ≈ W_sched обработчика);
// TEnd   — обработка закончена.
type Response struct {
	ID     uint64
	TRead  int64
	TStart int64
	TEnd   int64
}

func (r *Request) Encode(b []byte) {
	binary.LittleEndian.PutUint64(b[0:], r.ID)
	binary.LittleEndian.PutUint64(b[8:], r.ServiceNs)
	binary.LittleEndian.PutUint32(b[16:], r.AllocBytes)
	binary.LittleEndian.PutUint32(b[20:], 0)
}

func (r *Request) Decode(b []byte) {
	r.ID = binary.LittleEndian.Uint64(b[0:])
	r.ServiceNs = binary.LittleEndian.Uint64(b[8:])
	r.AllocBytes = binary.LittleEndian.Uint32(b[16:])
}

func (r *Response) Encode(b []byte) {
	binary.LittleEndian.PutUint64(b[0:], r.ID)
	binary.LittleEndian.PutUint64(b[8:], uint64(r.TRead))
	binary.LittleEndian.PutUint64(b[16:], uint64(r.TStart))
	binary.LittleEndian.PutUint64(b[24:], uint64(r.TEnd))
}

func (r *Response) Decode(b []byte) {
	r.ID = binary.LittleEndian.Uint64(b[0:])
	r.TRead = int64(binary.LittleEndian.Uint64(b[8:]))
	r.TStart = int64(binary.LittleEndian.Uint64(b[16:]))
	r.TEnd = int64(binary.LittleEndian.Uint64(b[24:]))
}

// ReadRequest читает ровно один кадр запроса.
func ReadRequest(r io.Reader, buf []byte, req *Request) error {
	if _, err := io.ReadFull(r, buf[:ReqSize]); err != nil {
		return err
	}
	req.Decode(buf)
	return nil
}

// ReadResponse читает ровно один кадр ответа.
func ReadResponse(r io.Reader, buf []byte, resp *Response) error {
	if _, err := io.ReadFull(r, buf[:RespSize]); err != nil {
		return err
	}
	resp.Decode(buf)
	return nil
}
