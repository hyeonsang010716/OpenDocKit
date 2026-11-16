# Privacy

OpenDocKit converts the file a workflow gives it, inside the plugin runtime of your own Dify deployment.

- **No outside service.** The only network request is the download of that file from your Dify instance (its
  `FILES_URL` or `INTERNAL_FILES_URL`). The conversion itself makes no network requests.
- **Nothing is shared.** No data is sent to the plugin author or to any third party. There is no analytics or tracking.
- **Nothing is kept.** The plugin stores no copy of the file or of its text after it returns the result.
- **No personal data is collected.** A document may contain personal data; its text goes only into the tool's output,
  inside your Dify.
- **Logs.** The plugin writes nothing from the document to the plugin runtime log: its error messages are fixed
  sentences, and the conversion warnings, which can name a part of the document such as a hidden sheet, are returned
  in the tool's output only.

Who can see the files and the workflow results is decided by the operator of the Dify deployment.
