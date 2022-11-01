#include "device/usbd.h"
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "pico/cyw43_arch.h"
#include "hardware/timer.h"
#include "hardware/i2c.h"
#include "hardware/spi.h"
#include "bsp/board.h"
#include "tusb.h"
#include "ssd1306_spi.h"
#include "as5600.h"

#include "ff.h"
#include "hw_config.h"
#include <stdint.h>

void create_button(uint, void *, void (*)(void *), void (*)(void *));

enum state {
    IDLING,
    SLEEPING,
    RECORDING,
    UPLOADING,
};

enum command {
    OPEN,
    DUMP,
    FINISH
};

static enum state state;
static ssd1306_t disp;
static repeating_timer_t data_acquisition_timer;
static FIL recording;

// ----------------------------------------------------------------------------
// Data acquisition

static const uint16_t SAMPLE_RATE = 1000;

static bool have_fork;
static bool have_shock;

struct header {
    char magic[3];
    uint8_t version;
    uint16_t sample_rate;
};

struct record {
    uint16_t fork_angle;
    uint16_t shock_angle;
};

// We are using two buffers. Data acquisition happens on core #1 into the active
// buffer (referred to by the pointer active_buffer) and we dump to Micro SD card
// on core #2.
//
// When the active buffer is filled on core #1,
//  - the buffer's pointer is sent to core #2 via the Pico's multicore FIFO
//  - the other buffer's address is read from the FIFO, and set as active buffer.
//  
// Core #2 waits until an address is sent from core #1, and
//  - dumps the content at that address to the card
//  - sends the buffer address to core #1 via FIFO 
// 

#define BUFFER_SIZE 2048 // Not declared as a static const, because variable
                         // length arrays are not a thing in C.

struct record databuffer1[BUFFER_SIZE];
struct record databuffer2[BUFFER_SIZE];
struct record *active_buffer = databuffer1;
uint16_t count = 0;

bool data_acquisition_cb(repeating_timer_t *rt) {
    if (count == BUFFER_SIZE) {
        count = 0;
        multicore_fifo_push_blocking(DUMP);
        multicore_fifo_push_blocking((uintptr_t)active_buffer);
        active_buffer = (struct record *)((uintptr_t)multicore_fifo_pop_blocking());
    }

    if (have_fork) {
        //XXX active_buffer[count].fork_angle = as5600_get_scaled_angle(i2c0);
        active_buffer[count].fork_angle = 0xcafe;
    } else {
        active_buffer[count].fork_angle = 0xffff;
    }

    if (have_shock) {
        //XXX active_buffer[count].shock_angle = as5600_get_scaled_angle(i2c1);
        active_buffer[count].shock_angle = 0xbabe;
    } else {
        active_buffer[count].shock_angle = 0xffff;
    }

    count += 1;

    return state == RECORDING; // keep repeating
}

// ----------------------------------------------------------------------------
// Data storage

int setup_storage() {
    sd_card_t *sd = sd_get_by_num(0);
    FRESULT fr = f_mount(&sd->fatfs, sd->pcName, 1);
    if (fr != FR_OK) {
        return PICO_ERROR_GENERIC;
    }

    fr = f_mkdir("uploaded");
    if (!(fr == FR_OK || fr == FR_EXIST)) {
        return PICO_ERROR_GENERIC;
    }

    return 0;
}

int open_datafile() {
    uint16_t index = 0;
    FIL index_fil;
    FRESULT fr = f_open(&index_fil, "INDEX", FA_OPEN_EXISTING | FA_READ);
    if (fr == FR_OK || fr == FR_EXIST) {
        uint8_t buf[2];
        uint br;
        f_read(&index_fil, buf, 2, &br);
        if (br == 2) {
            index = ((buf[0] << 8) | buf[1]) + 1;
        }
    }
    f_close(&index_fil);

    fr = f_open(&index_fil, "INDEX", FA_OPEN_ALWAYS | FA_WRITE);
    if (fr == FR_OK) {
        f_lseek(&index_fil, 0);
        uint8_t buf[2] = {
            (index >> 8) & 0xff,
            index & 0xff
        };
        uint bw;
        f_write(&index_fil, buf, 2, &bw);
        f_close(&index_fil);
    } else {
        return PICO_ERROR_GENERIC;
    }

    char filename[10];
    sprintf(filename, "%05u.SST", index);
    fr = f_open(&recording, filename, FA_CREATE_NEW | FA_WRITE);
    if (fr != FR_OK) {
        return fr;
    }

    struct header h = {"SST", 2, SAMPLE_RATE};
    f_write(&recording, &h, sizeof(struct header), NULL);

    return index;
}

void data_storage_core1() {
    int err = setup_storage();
    multicore_fifo_push_blocking(err);

    int index;
    enum command cmd;
    uint16_t size;
    struct record *buffer;
    while (true) {
        cmd = (enum command)multicore_fifo_pop_blocking();
        switch(cmd) {
            case OPEN:
                multicore_fifo_drain();
                index = open_datafile();
                multicore_fifo_push_blocking(index);
                multicore_fifo_push_blocking((uintptr_t)databuffer2);
                break;
            case DUMP:
                buffer = (struct record *)((uintptr_t)multicore_fifo_pop_blocking());
                multicore_fifo_push_blocking((uintptr_t)buffer);
                f_write(&recording, buffer, sizeof(struct record)*BUFFER_SIZE, NULL);
                f_sync(&recording);
                break;
            case FINISH:
                size = (uint16_t)multicore_fifo_pop_blocking();
                buffer = (struct record *)((uintptr_t)multicore_fifo_pop_blocking());
                f_write(&recording, buffer, sizeof(struct record)*size, NULL);
                f_sync(&recording);
                f_close(&recording);
                break;
        }
    }
}

// ----------------------------------------------------------------------------
// Setup functions

void setup_i2c() {
    i2c_init(i2c0, 1000000);
    gpio_set_function(20, GPIO_FUNC_I2C);
    gpio_set_function(21, GPIO_FUNC_I2C);
    gpio_pull_up(20);
    gpio_pull_up(21);

    i2c_init(i2c1, 1000000);
    gpio_set_function(26, GPIO_FUNC_I2C);
    gpio_set_function(27, GPIO_FUNC_I2C);
    gpio_pull_up(26);
    gpio_pull_up(27);
}

bool setup_baseline(i2c_inst_t *i2c) {
    if (as5600_connected(i2c) && as5600_detect_magnet(i2c)) {
        uint16_t baseline = 0;
        for (int i = 0; i < 10; ++i) {
            baseline += as5600_get_raw_angle(i2c);
            sleep_ms(10);
        }
        baseline /= 10;
        as5600_set_start_position(i2c, baseline);

        // Power down tha DAC, we don't need it.
        as5600_conf_set_output(i2c, OUTPUT_PWM);
        // Helps with those 1-quanta-high rapid spikes.
        as5600_conf_set_hysteresis(i2c, HYSTERESIS_2_LSB);
        // 0.55 ms step response delay, 0.03 RMS output noise.
        as5600_conf_set_slow_filter(i2c, SLOW_FILTER_4x);
        // TODO: experiment with fast filter.
        as5600_conf_set_fast_filter_threshold(i2c, FAST_FILTER_THRESHOLD_6_LSB);
        return true;
    } else {
        return false;
    }
}

void setup_sensors() {
    /* XXX
    uint8_t dummy;
    while (!((as5600_connected(i2c0) && as5600_detect_magnet(i2c0)) ||
            (as5600_connected(i2c1) && as5600_detect_magnet(i2c1)))) {
        sleep_ms(500);
    }

    have_fork = setup_baseline(i2c0);
    have_shock = setup_baseline(i2c1);
    */
    
    have_fork = true;
    have_shock = true;
}

void setup_display(ssd1306_t *disp) {
    spi_init(spi0, 1000000);
    gpio_set_function(18, GPIO_FUNC_SPI); // SCK
    gpio_set_function(19, GPIO_FUNC_SPI); // MOSI

    disp->external_vcc = false;
    ssd1306_init(disp, 128, 32, spi0,
            17,  // CS
            16,  // DC
            22); // RST
            
    ssd1306_clear(disp);
    ssd1306_show(disp);
}

// ----------------------------------------------------------------------------
// Helper functions

void display_message(ssd1306_t *disp, char *message) {
    ssd1306_clear(disp);
    ssd1306_draw_string(disp, 8, 8, 2, message);
    ssd1306_show(disp);
}

bool msc_present() {
    // WL_GPIO2 is VBUS sense. WL_GPIO2 low -> no USB cable -> no MSC.
    if (cyw43_arch_gpio_get(2)) {
        // Wait for a maximum of 1 second for USB MSC to initialize
        uint32_t t = time_us_32();
        while (!tud_ready()) {
            if (time_us_32() - t > 1000000) {
                return false;
            }
            tud_task();
        }
        return true;
    }

    return false;
}

// ----------------------------------------------------------------------------
// Button handlers

void start_recording() {
    count = 0;
    active_buffer = databuffer1;
    multicore_fifo_drain();
    
    multicore_fifo_push_blocking(OPEN);
    int index = (int)multicore_fifo_pop_blocking();
    if (index < 0) {
        display_message(&disp, "FILE ERR");
        while(true) { tight_loop_contents(); }
    }

    // Start data acquisition timer
    if (!add_repeating_timer_us(-1000000/SAMPLE_RATE, data_acquisition_cb, NULL, &data_acquisition_timer)) {
        display_message(&disp, "TIMER ERR");
        while(true) { tight_loop_contents(); }
    }
}

void stop_recording() {
    cancel_repeating_timer(&data_acquisition_timer);

    multicore_fifo_push_blocking(FINISH);
    multicore_fifo_push_blocking(count);
    multicore_fifo_push_blocking((uintptr_t)active_buffer);
}

void on_left_press(void *user_data) {
    switch(state) {
        case IDLING:
            state = RECORDING;
            start_recording();
            char msg[8];
            sprintf(msg, "REC:%s|%s", have_fork ? "F" : ".", have_shock ? "S" : ".");
            display_message(&disp, msg);
            break;
        case RECORDING:
            state = IDLING;
            stop_recording();
            display_message(&disp, "IDLE");
            break;
        default:
            break;
    }
}

// ----------------------------------------------------------------------------
// Entry point 

int main() {
    setup_i2c();
    board_init();
    cyw43_arch_init();
    tusb_init();

    setup_display(&disp);

    if (msc_present()) {
        display_message(&disp, "MSC MODE");
        while(true) { tud_task(); }
    } else {
        display_message(&disp, "INIT STOR");
        multicore_launch_core1(&data_storage_core1);
        int err = (int)multicore_fifo_pop_blocking();
        if (err < 0) {
            display_message(&disp, "CARD ERR");
            while(true) { tight_loop_contents(); }
        }

        display_message(&disp, "INIT SENS");
        setup_sensors();
    
        display_message(&disp, "IDLE");
        state = IDLING;

        create_button(1, NULL, on_left_press, NULL);

        while (true) { tight_loop_contents(); }
    }

    return 0;
}
