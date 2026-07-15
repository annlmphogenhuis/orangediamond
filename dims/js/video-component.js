window.TimeRangeVideo = function TimeRangeVideo(props) {
    const { React } = window;

    const videoRef = React.useRef(null);

    React.useEffect(() => {
        const video = videoRef.current;
        if (!video || props.startTime === undefined) return;

        const doSeek = () => { video.currentTime = props.startTime; };

        if (video.readyState >= 1) {
            doSeek();
        } else {
            // loadedmetadata fires reliably even with preload="metadata"
            video.addEventListener('loadedmetadata', doSeek, { once: true });
        }
    }, [props.startTime, props.src]);

    const handleTimeUpdate = (e) => {
        if (props.endTime !== undefined && e.target.currentTime >= props.endTime) {
            e.target.pause();
            e.target.currentTime = props.startTime !== undefined ? props.startTime : 0;
        }
    };

    return React.createElement('div', { className: 'video-player-container' },
        React.createElement('h3', { style: { color: 'white' } }, props.title),
        React.createElement('video', {
            ref: videoRef,
            src: props.src,
            controls: true,
            style: { width: '100%' },
            onTimeUpdate: handleTimeUpdate,
            preload: 'metadata'
        })
    );
};