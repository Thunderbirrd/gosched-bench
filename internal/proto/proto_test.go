package proto

import (
	"bytes"
	"testing"
)

func TestRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	b := make([]byte, RespSize)
	in := Request{ID: 42, ServiceNs: 50_000, AllocBytes: 1024}
	in.Encode(b)
	buf.Write(b[:ReqSize])
	var out Request
	if err := ReadRequest(&buf, b, &out); err != nil || out != in {
		t.Fatalf("request: %+v != %+v (%v)", out, in, err)
	}
	rin := Response{ID: 7, TRead: 1, TStart: 2, TEnd: -3}
	rin.Encode(b)
	buf.Write(b[:RespSize])
	var rout Response
	if err := ReadResponse(&buf, b, &rout); err != nil || rout != rin {
		t.Fatalf("response: %+v != %+v (%v)", rout, rin, err)
	}
}
