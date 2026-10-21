# Builds the Linux x86-64 usb_boot. Run `make docker` on any host with Docker; it
# uses Ubuntu 14.04 (GCC 4.8.2, libusb 1.0.17), the toolchain of the original binary.
CXX      ?= g++
CXXFLAGS ?= -std=gnu++11 -O0 -Wall
LDLIBS    = -lusb-1.0 -lpthread

usb_boot: src/usb_boot.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDLIBS)

docker:
	docker run --rm --platform linux/amd64 -v "$(CURDIR)":/src -w /src ubuntu:14.04 \
	  sh -c 'apt-get update -qq && apt-get install -y -qq g++ libusb-1.0-0-dev make && make clean usb_boot'

clean:
	rm -f usb_boot

.PHONY: docker clean
