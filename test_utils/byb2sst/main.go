package main

import (
	"bytes"
	"encoding/base64"
	"encoding/csv"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path"
	"strconv"
	"strings"
	"time"

	"github.com/jessevdk/go-flags"
	"github.com/ugorji/go/codec"

	psst "github.com/sghctoma/sst/gosst/formats/psst"
)

type session struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	RawData     string `json:"data"`
}

type response struct {
	Id    int    `json:"id"`
	Error string `json:"error"`
}

type ApiError struct {
	ErrorMessage string
}

func (e *ApiError) Error() string {
	return e.ErrorMessage
}

func loadData(file string, start int64) ([]float64, []float64, error) {
	f, err := os.Open(file)
	if err != nil {
		return nil, nil, err
	}
	defer f.Close()

	r := csv.NewReader(f)
	r.Comma = ';'

	rows, err := r.ReadAll()
	if err != nil {
		return nil, nil, err
	}

	rows = rows[1:]
	record_count := len(rows)
	fork := make([]float64, record_count)
	shock := make([]float64, record_count)
	for idx := range rows {
		fork[idx], _ = strconv.ParseFloat(rows[idx][1], 64)
		shock[idx], _ = strconv.ParseFloat(rows[idx][2], 64)
	}

	return fork, shock, nil
}

func createLinkage(file string, angle, fmax, rmax float64) (*psst.Linkage, error) {
	var linkage psst.Linkage
	basename := path.Base(file)
	linkage.Name = strings.TrimSuffix(basename, path.Ext(basename))
	linkage.HeadAngle = angle
	linkage.MaxFrontStroke = fmax
	linkage.MaxRearStroke = rmax

	f, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	r := csv.NewReader(f)
	r.Comma = ';'

	rows, err := r.ReadAll()
	if err != nil {
		return nil, err
	}

	rows = rows[1:]
	var records []psst.LinkageRecord
	for idx := range rows {
		s, _ := strconv.ParseFloat(rows[idx][0], 64)
		w, _ := strconv.ParseFloat(rows[idx][1], 64)
		var l float64
		if idx > 0 {
			sdiff := s - records[idx-1].ShockTravel
			wdiff := w - records[idx-1].WheelTravel
			l = wdiff / sdiff
			records[idx-1].LeverageRatio = l
		}
		records = append(records, psst.LinkageRecord{
			ShockTravel:   s,
			WheelTravel:   w,
			LeverageRatio: l, // this will be overwritten in the next run, except for the last row
		})
	}

	linkage.Process(records)
	return &linkage, nil
}

func createCalibrations(linkage *psst.Linkage) (*psst.Calibration, *psst.Calibration, error) {
	method := psst.CalibrationMethod{Name: "percentage"}
	method.Inputs = []string{}
	method.Intermediates = map[string]string{"factor": "MAX_STROKE / 100.0"}
	method.Expression = "sample * factor"
	fcal := psst.Calibration{
		Name:   "Percentage",
		Method: &method,
		Inputs: map[string]float64{},
	}
	if err := fcal.Prepare(linkage.MaxFrontStroke, linkage.MaxFrontTravel); err != nil {
		return nil, nil, err
	}
	rcal := psst.Calibration{
		Name:   "Percentage",
		Method: &method,
		Inputs: map[string]float64{},
	}
	if err := rcal.Prepare(linkage.MaxRearStroke, linkage.MaxRearTravel); err != nil {
		return nil, nil, err
	}
	return &fcal, &rcal, nil
}

func putSession(session session, url string, token string) error {
	sessionJson, err := json.Marshal(session)
	if err != nil {
		return err
	}
	req, err := http.NewRequest("PUT", url+"/session/psst", bytes.NewBuffer(sessionJson))
	if err != nil {
		log.Fatalln(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Token", token)
	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	var r response
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return err
	}
	if r.Error != "" {
		return &ApiError{ErrorMessage: r.Error}
	}
	return nil
}

func main() {
	var opts struct {
		BybFile        string  `short:"b" long:"bybfile" description:"BYB CSV data file" required:"true"`
		Time           string  `short:"d" long:"datetime" description:"Session start time in UTC (YYYY-MM-DD HH:mm:ss)"`
		LeverageFile   string  `short:"l" long:"leverage" description:"BYB leverage file" required:"true"`
		HeadAngle      float64 `short:"a" long:"headangle" description:"Head tube angle (deg)" required:"true"`
		MaxFrontStroke float64 `short:"f" long:"frontstroke" description:"Maximum front stroke (mm)" required:"true"`
		MaxRearStroke  float64 `short:"r" long:"rearstroke" description:"Maximum rear stroke (mm)" required:"true"`
		ApiUrl         string  `short:"g" long:"gosstapi" description:"GoSST HTTP API URL" default:"http://localhost:8080"`
		ApiToken       string  `short:"t" long:"token" description:"GoSST HTTP API token" required:"true"`
	}
	_, err := flags.Parse(&opts)
	if err != nil {
		return
	}

	linkage, err := createLinkage(opts.LeverageFile, opts.HeadAngle, opts.MaxFrontStroke, opts.MaxRearStroke)
	if err != nil {
		log.Fatalln(err)
	}

	fcal, rcal, err := createCalibrations(linkage)
	if err != nil {
		log.Fatalln(err)
	}

	basename := path.Base(opts.BybFile)
	sessionName := strings.TrimSuffix(basename, path.Ext(basename))
	start, err := time.Parse("2006-01-02 15:04:05", opts.Time)
	if err != nil {
		start = time.Now()
	}
	meta := psst.Meta{
		Name:       sessionName,
		Version:    1,
		SampleRate: 1000,
		Timestamp:  start.Unix(),
	}

	fork, shock, err := loadData(opts.BybFile, start.Unix())
	if err != nil {
		log.Fatalln(err)
	}

	pd, err := psst.ProcessRecording(fork, shock, meta, *linkage, *fcal, *rcal)
	if err != nil {
		log.Fatalln(err)
	}

	var psstBytes []byte
	var h codec.MsgpackHandle
	enc := codec.NewEncoderBytes(&psstBytes, &h)
	enc.Encode(pd)

	session := session{
		Name:        sessionName,
		Description: "imported from " + opts.BybFile,
		RawData:     base64.StdEncoding.EncodeToString(psstBytes),
	}
	if err := putSession(session, opts.ApiUrl, opts.ApiToken); err != nil {
		log.Fatalln(err)
	}
}